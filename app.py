"""
DC Audit Report Generator v2.2
Physical audit sheet generator with PDF export for inventory verification

CHANGELOG:
v2.2 (2026-10-07)
- "Pull from Freshdesk" button on each ticket: fills the ticket #, comments, and
  SKU search (product, batch, Metrc tag) from the Freshdesk ticket. API key lives
  in .streamlit/secrets.toml (git-ignored)

v2.1 (2026-09-29)
- Added "Ticket Auditing" section (sidebar): one box per ticket with ticket #,
  pasted ticket comments, and SKU / batch / package label search
- Zero-quantity items are listed but unticked by default (checkbox to print all)
- Selling Qty and Total Qty (Active + Selling) columns for packages in "selling"
  status (Full and Ticket audits); warns when an export has no selling packages
- Ticket audit PDFs: tickets print back to back (several per page, never split
  unless longer than a page), optional one-per-page, no signature lines
- Optional Product List CSV: search by product SKU, and sold-out products with
  no packages still print (0 qty, "No packages in export")
- SKUs missing from the export (and product list) still print, as "Not in export" rows
- Uploading a new CSV now re-processes the data

v2.0 (2025-11-07)
- Redesigned with "Report Builder" workflow
- Added PDF generation for physical audits
- Moved filters to main content area
- Added audit-friendly formatting with checkboxes
- Signature lines and audit metadata

v1.0 (2025-11-07)
- Initial release
"""

import streamlit as st
import pandas as pd
import io
import re
import difflib
import requests
from xml.sax.saxutils import escape as xml_escape
from reportlab.lib.pagesizes import letter, A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak, KeepTogether, HRFlowable
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from datetime import datetime

# ============================================================================
# CONFIGURATION
# ============================================================================

# Page config
st.set_page_config(
    page_title="DC Audit Report v2.2",
    page_icon="📋",
    layout="wide"
)

# Version and constants
VERSION = "2.2"

# Category / batch label for searched SKUs that have no packages in the export
NOT_IN_EXPORT = "Not in export"
# Batch label for products in the product list that have no packages in the export
NO_PACKAGES = "No packages in export"

# ============================================================================
# UTILITY FUNCTIONS
# ============================================================================

def extract_brand_from_product(product_name):
    """
    Extract brand from product name (everything before ' - ')
    
    Args:
        product_name (str): Full product name like "Pretty Dope - 24k Gold Vape 1g"
        
    Returns:
        str: Brand name or 'Unknown' if not found
    """
    if pd.isna(product_name):
        return 'Unknown'
    
    name_str = str(product_name).strip()
    if ' - ' in name_str:
        brand = name_str.split(' - ')[0].strip()
        return brand if brand else 'Unknown'
    else:
        return 'Unknown'

def safe_numeric(value, default=0):
    """Safely convert value to numeric, return default if fails"""
    try:
        return float(value) if pd.notna(value) else default
    except:
        return default

# ============================================================================
# DATA LOADING FUNCTIONS
# ============================================================================

def load_packages_csv(uploaded_file):
    """
    Load packages CSV file
    
    Args:
        uploaded_file: Streamlit UploadedFile object
        
    Returns:
        DataFrame or None if error
    """
    try:
        df = pd.read_csv(uploaded_file)
        return df
    except Exception as e:
        st.error(f"Error loading CSV: {str(e)}")
        return None

# ============================================================================
# DATA PROCESSING FUNCTIONS
# ============================================================================

def validate_required_columns(df):
    """Validate that DataFrame has required columns for audit processing"""
    if df is None or df.empty:
        return False, "DataFrame is empty"
    
    required_cols = ['Distru Product', 'Category', 'Distru Batch Number', 'Available Quantity']
    missing = [col for col in required_cols if col not in df.columns]
    
    if missing:
        return False, f"Missing required columns: {', '.join(missing)}"
    
    return True, "All required columns present"

def process_packages_to_audit(df):
    """
    Process packages DataFrame into audit sheet format
    
    Returns:
        DataFrame: Processed audit data with Brand column
    """
    # Make a copy
    audit_df = df.copy()
    
    # Extract brand
    audit_df['Brand'] = audit_df['Distru Product'].apply(extract_brand_from_product)
    
    # Convert quantity to numeric
    available = audit_df['Available Quantity'].apply(lambda x: safe_numeric(x, 0))

    # Packages in "selling" status are counted separately from active stock.
    # Their full package Quantity is used, since units being sold are still on hand.
    if 'Status' in audit_df.columns:
        is_selling = audit_df['Status'].astype(str).str.strip().str.lower() == 'selling'
    else:
        is_selling = pd.Series(False, index=audit_df.index)
    qty_col = 'Quantity' if 'Quantity' in audit_df.columns else 'Available Quantity'
    package_qty = audit_df[qty_col].apply(lambda x: safe_numeric(x, 0))

    audit_df['System_Qty'] = available.where(~is_selling, 0)
    audit_df['Selling_Qty'] = package_qty.where(is_selling, 0)

    # Group by Category, Product, and Batch Number, sum quantities
    # (package labels are kept so Ticket Auditing can search by Metrc tag)
    agg_spec = {'System_Qty': 'sum', 'Selling_Qty': 'sum'}
    if 'Package Label' in audit_df.columns:
        agg_spec['Package Label'] = lambda labels: ' '.join(labels.dropna().astype(str))
    grouped = audit_df.groupby(
        ['Category', 'Brand', 'Distru Product', 'Distru Batch Number'],
        dropna=False
    ).agg(agg_spec).reset_index()
    
    # Round quantities to integers
    grouped['System_Qty'] = grouped['System_Qty'].round().astype(int)
    grouped['Selling_Qty'] = grouped['Selling_Qty'].round().astype(int)

    # Sort by Category, then Brand, then Product
    grouped = grouped.sort_values(['Category', 'Brand', 'Distru Product', 'Distru Batch Number'])
    
    return grouped

def parse_search_terms(raw_text):
    """Split search box text into terms (one per line or comma-separated)"""
    if not raw_text:
        return []
    terms = []
    for line in raw_text.replace(',', '\n').splitlines():
        term = line.strip()
        if term and term.lower() not in [t.lower() for t in terms]:
            terms.append(term)
    return terms

def words_mask(haystack, term):
    """Rows of a lower-cased text Series that contain every word of the term"""
    mask = pd.Series(True, index=haystack.index)
    for word in term.lower().split():
        mask &= haystack.str.contains(word, regex=False)
    return mask

def search_audit_items(audit_df, terms):
    """
    Find audit rows matching any search term.

    A term matches a row when every word in the term appears somewhere in the
    product name, batch number, package label, or product SKU (case-insensitive),
    so "wedding cake almora" finds "Almora - Wedding Cake 3.5g".

    Returns:
        (DataFrame of matched rows, dict of term -> boolean mask)
    """
    haystack = (
        audit_df['Distru Product'].fillna('').astype(str) + ' ' +
        audit_df['Distru Batch Number'].fillna('').astype(str)
    )
    for col in ['Package Label', 'SKU']:
        if col in audit_df.columns:
            haystack = haystack + ' ' + audit_df[col].fillna('').astype(str)
    haystack = haystack.str.lower()

    term_masks = {}
    combined = pd.Series(False, index=audit_df.index)
    for term in terms:
        mask = words_mask(haystack, term)
        term_masks[term] = mask
        combined |= mask

    return audit_df[combined], term_masks

def load_product_list_csv(uploaded_file):
    """
    Load the Distru products export (optional second upload)

    Returns:
        DataFrame with Name, SKU, Category, or None if unreadable
    """
    try:
        products = pd.read_csv(uploaded_file, low_memory=False)
    except Exception as e:
        st.sidebar.error(f"Error loading product list: {str(e)}")
        return None

    if 'Name' not in products.columns:
        st.sidebar.error("❌ Product list is missing the 'Name' column")
        return None

    for col in ['SKU', 'Category']:
        if col not in products.columns:
            products[col] = ''
    products = products[['Name', 'SKU', 'Category']].dropna(subset=['Name']).copy()
    products['SKU'] = products['SKU'].fillna('').astype(str).str.strip()
    products['Category'] = products['Category'].fillna('Unknown')
    return products

def attach_product_skus(audit_df, products_df):
    """Add each package row's product SKU (from the product list) for searching and display"""
    if products_df is None:
        return audit_df
    name_to_sku = products_df.drop_duplicates('Name').set_index('Name')['SKU']
    return audit_df.assign(SKU=audit_df['Distru Product'].map(name_to_sku).fillna(''))

def product_list_rows(products_df, terms):
    """
    Rows for searched products that exist in Distru but have no packages in the export.

    A product whose name or SKU exactly equals the term starts ticked; otherwise a
    term's matches are only ticked when it found a single product.

    Returns:
        (rows DataFrame, include flags Series, terms that were found) or (None, None, [])
    """
    haystack = (products_df['Name'].astype(str) + ' ' + products_df['SKU']).str.lower()
    hits_list, ticks_list, found = [], [], []
    for term in terms:
        hits = products_df[words_mask(haystack, term)]
        if hits.empty:
            continue
        found.append(term)
        exact = (hits['Name'].str.lower() == term.lower()) | (hits['SKU'].str.lower() == term.lower())
        ticks_list.append(exact if exact.any() else pd.Series(len(hits) == 1, index=hits.index))
        hits_list.append(hits)

    if not hits_list:
        return None, None, []

    # A product found by two terms is listed once, ticked if either term wanted it
    ticks = pd.concat(ticks_list).groupby(level=0).max()
    hits = pd.concat(hits_list)
    hits = hits[~hits.index.duplicated()].sort_values(['Category', 'Name'])

    index = [f"product_{i}" for i in hits.index]
    rows = pd.DataFrame(
        {
            'Category': hits['Category'].values,
            'Brand': [extract_brand_from_product(n) for n in hits['Name']],
            'Distru Product': hits['Name'].values,
            'Distru Batch Number': NO_PACKAGES,
            'System_Qty': 0,
            'Selling_Qty': 0,
            'SKU': hits['SKU'].values,
        },
        index=index
    )
    return rows, pd.Series(ticks.loc[hits.index].values, index=index), found

def suggest_products(product_names, term, limit=3):
    """Closest product names to a search term, to help spot typos"""
    by_lower = {str(p).lower(): p for p in product_names}
    matches = difflib.get_close_matches(term.lower(), list(by_lower), n=limit, cutoff=0.6)
    return [by_lower[m] for m in matches]

def not_in_export_rows(terms):
    """Placeholder audit rows for searched SKUs, tags or batches missing from the export"""
    return pd.DataFrame(
        {
            'Category': NOT_IN_EXPORT,
            'Brand': [extract_brand_from_product(t) for t in terms],
            'Distru Product': terms,
            'Distru Batch Number': NOT_IN_EXPORT,
            'System_Qty': 0,
            'Selling_Qty': 0,
            'SKU': '',
        },
        index=[f"not_in_export_{i}" for i in range(len(terms))]
    )

def count_real_batches(df):
    """Unique batches, ignoring placeholder rows for products with no packages"""
    batches = df['Distru Batch Number']
    return batches[~batches.isin([NO_PACKAGES, NOT_IN_EXPORT])].nunique()

def has_selling_packages(audit_df):
    """True when the export contains any packages in selling status"""
    return bool((audit_df['Selling_Qty'] != 0).any())

# ============================================================================
# FRESHDESK FUNCTIONS
# ============================================================================

# Metrc package tags are 24 characters starting with 1A4
METRC_TAG_RE = re.compile(r'\b1A4[0-9A-Z]{21}\b', re.IGNORECASE)
BATCH_RE = re.compile(r'\bBatch(?:\s*(?:#|No\.?|Number))?\s*[:#]\s*([A-Za-z0-9][A-Za-z0-9._\-]*)', re.IGNORECASE)
# "5 Missing units of Stiiizy - King Louis XIII Pod 1g at Haven Paramount ..."
# "We are missing 50 units - Cizi - Glitter Bomb Preroll 1g from 9/24 delivery ..."
PRODUCT_RE = re.compile(
    r'\bunits?\s*(?:of|[-–:])\s+(.+?)(?:\s+(?:at|from)\s+|\s+Batch\b|\s+Metrc\b|[\r\n]|$)',
    re.IGNORECASE
)

def freshdesk_config():
    """
    Freshdesk domain and API key from .streamlit/secrets.toml (or Streamlit Cloud secrets)

    Returns:
        (domain, api_key) or None when not set up
    """
    try:
        section = st.secrets["freshdesk"]
        domain = str(section.get("domain", "")).strip()
        api_key = str(section.get("api_key", "")).strip()
    except Exception:
        return None
    if not domain or not api_key or api_key.startswith("PASTE-"):
        return None
    domain = re.sub(r'^https?://', '', domain).strip('/')
    return domain, api_key

def parse_ticket_number(text):
    """Ticket number from '17948', '#17948' or a full Freshdesk ticket URL"""
    match = re.search(r'(\d+)\s*/?\s*$', str(text or '').strip())
    return match.group(1) if match else None

@st.cache_data(ttl=300, show_spinner=False)
def fetch_freshdesk_ticket(domain, api_key, ticket_number):
    """
    Read one ticket from the Freshdesk API (v2)

    Returns:
        dict with subject / description_text / etc.
    Raises:
        RuntimeError with a message suitable for the UI
    """
    url = f"https://{domain}/api/v2/tickets/{ticket_number}"
    try:
        response = requests.get(url, auth=(api_key, "X"), timeout=15)
    except requests.RequestException as e:
        raise RuntimeError(f"Couldn't reach Freshdesk ({e.__class__.__name__}). Check your connection.")

    if response.status_code == 200:
        return response.json()
    if response.status_code == 401:
        raise RuntimeError("Freshdesk rejected the API key. Check it in .streamlit/secrets.toml.")
    if response.status_code == 403:
        raise RuntimeError("Your Freshdesk account doesn't have access to this ticket.")
    if response.status_code == 404:
        raise RuntimeError(f"Ticket #{ticket_number} wasn't found in Freshdesk.")
    if response.status_code == 429:
        wait = response.headers.get('Retry-After', 'a minute')
        raise RuntimeError(f"Freshdesk rate limit hit. Try again in {wait} seconds.")
    raise RuntimeError(f"Freshdesk returned an error (HTTP {response.status_code}).")

def ticket_comment_text(ticket):
    """Subject plus plain-text description, for the Ticket comments box"""
    subject = str(ticket.get('subject') or '').strip()
    description = str(ticket.get('description_text') or '').strip()
    # Collapse the blank-line runs Freshdesk leaves in plain-text descriptions
    description = re.sub(r'\n\s*\n+', '\n', description)
    if subject and description and not description.lower().startswith(subject.lower()):
        return f"{subject}\n{description}"
    return description or subject

def extract_search_terms(text):
    """
    Pull product names, batch numbers and Metrc tags out of ticket text,
    e.g. "5 Missing units of Stiiizy - King Louis XIII Pod 1g at Haven Paramount
    Batch: ST-ORG-KLO-G1026-V3P Metrc: 1A406030004FBC9000244991"
    """
    terms = []

    def add(term):
        term = term.strip().strip('.,;:')
        if term and term.lower() not in [t.lower() for t in terms]:
            terms.append(term)

    for match in PRODUCT_RE.finditer(text):
        add(match.group(1))
    for match in BATCH_RE.finditer(text):
        add(match.group(1))
    for match in METRC_TAG_RE.finditer(text):
        add(match.group(0).upper())
    return terms

def clean_ticket_ref(ticket_ref):
    """Ticket # for the sheet: '#17948' -> '17948', a Freshdesk link -> its number"""
    ref = str(ticket_ref or '').strip()
    if '/' in ref:
        ref = parse_ticket_number(ref) or ref
    return ref.lstrip('#').strip()

def pull_freshdesk_ticket(key):
    """
    Button callback: fill a ticket box from Freshdesk.

    Runs before the widgets are drawn, so it may set their values.
    """
    status_key = f"{key}_fd_status"
    config = freshdesk_config()
    if config is None:
        st.session_state[status_key] = ('error', "Freshdesk isn't set up. Add your API key to .streamlit/secrets.toml.")
        return

    ticket_number = parse_ticket_number(st.session_state.get(f"{key}_ref", ''))
    if not ticket_number:
        st.session_state[status_key] = ('error', "Enter a ticket number (or paste the ticket link) first.")
        return

    try:
        ticket = fetch_freshdesk_ticket(*config, ticket_number)
    except RuntimeError as e:
        st.session_state[status_key] = ('error', str(e))
        return

    comments = ticket_comment_text(ticket)
    terms = extract_search_terms(comments)

    st.session_state[f"{key}_ref"] = ticket_number
    st.session_state[f"{key}_comments"] = comments
    if terms:
        st.session_state[f"{key}_search"] = "\n".join(terms)
        st.session_state[status_key] = ('success', f"Pulled ticket #{ticket_number}. "
                                        "Check the SKU search it filled in.")
    else:
        st.session_state[status_key] = ('warning', f"Pulled ticket #{ticket_number}, but couldn't spot a product, "
                                        "batch or Metrc tag in it. Enter the SKUs yourself.")

# ============================================================================
# PDF GENERATION FUNCTIONS
# ============================================================================

def get_pdf_styles():
    """Shared paragraph styles for audit PDFs"""
    styles = getSampleStyleSheet()
    return {
        'base': styles,
        'title': ParagraphStyle(
            'CustomTitle',
            parent=styles['Heading1'],
            fontSize=16,
            textColor=colors.HexColor('#1f77b4'),
            spaceAfter=6,
            alignment=TA_CENTER
        ),
        'header': ParagraphStyle(
            'CustomHeader',
            parent=styles['Normal'],
            fontSize=10,
            spaceAfter=12,
            alignment=TA_LEFT
        ),
        'cell': ParagraphStyle(
            'CellText',
            parent=styles['Normal'],
            fontSize=8,
            leading=10
        ),
        'comments': ParagraphStyle(
            'TicketComments',
            parent=styles['Normal'],
            fontSize=9,
            leading=12,
            backColor=colors.HexColor('#f5f7fa'),
            borderColor=colors.HexColor('#c8d0da'),
            borderWidth=0.5,
            borderPadding=6,
            spaceBefore=4,
            spaceAfter=14
        ),
    }

def build_category_tables(df, pdf_styles, show_selling=False,
                          page_break_per_category=True, category_heading='Heading2'):
    """
    Build one audit table per category

    Returns:
        list: Flowables (headers, tables, spacers, page breaks)
    """
    elements = []
    styles = pdf_styles['base']
    cell_style = pdf_styles['cell']
    # SKUs missing from the export print last
    categories = sorted(df['Category'].unique(), key=lambda c: (c == NOT_IN_EXPORT, str(c)))

    for cat_idx, category in enumerate(categories):
        cat_data = df[df['Category'] == category].copy()

        # Category header
        cat_header = Paragraph(
            f"<b>Category: {xml_escape(str(category))}</b> ({len(cat_data)} items)",
            styles[category_heading]
        )
        elements.append(cat_header)
        elements.append(Spacer(1, 0.1*inch))

        # Build table data - removed checkbox, brand, variance columns
        # With selling packages, Total (Active + Selling) is what should physically be there
        if show_selling:
            table_data = [['Product', 'Batch #', 'Active\nQty', 'Selling\nQty', 'Total\nQty', 'Physical\nCount']]
        else:
            table_data = [['Product', 'Batch #', 'System\nQty', 'Physical\nCount']]

        for _, row in cat_data.iterrows():
            # Product name - truncate if too long
            product = str(row['Distru Product'])
            max_len = 46 if show_selling else 60
            if len(product) > max_len:
                product = product[:max_len - 3] + "..."

            # Batch number as Paragraph to enable wrapping
            batch_para = Paragraph(xml_escape(str(row['Distru Batch Number'])), cell_style)

            data_row = [product, batch_para, str(row['System_Qty'])]
            if show_selling:
                data_row.append(str(row['Selling_Qty']))
                data_row.append(str(row['System_Qty'] + row['Selling_Qty']))
            data_row.append('')  # Empty for physical count
            table_data.append(data_row)

        # More space for Product, adequate space for Batch (with wrapping)
        if show_selling:
            col_widths = [3.0*inch, 1.45*inch, 0.65*inch, 0.65*inch, 0.65*inch, 0.9*inch]
        else:
            col_widths = [4*inch, 1.5*inch, 0.7*inch, 0.9*inch]

        table = Table(table_data, colWidths=col_widths, repeatRows=1)

        # Table style
        table.setStyle(TableStyle([
            # Header row
            ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#1f77b4')),
            ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
            ('ALIGN', (0, 0), (-1, 0), 'CENTER'),
            ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
            ('FONTSIZE', (0, 0), (-1, 0), 9),
            ('BOTTOMPADDING', (0, 0), (-1, 0), 8),
            ('TOPPADDING', (0, 0), (-1, 0), 8),

            # Data rows
            ('FONTNAME', (0, 1), (-1, -1), 'Helvetica'),
            ('FONTSIZE', (0, 1), (-1, -1), 8),
            ('ALIGN', (2, 1), (-1, -1), 'CENTER'),  # Center qty columns
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),  # Top alignment for wrapping
            ('TOPPADDING', (0, 1), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 1), (-1, -1), 6),

            # Grid
            ('GRID', (0, 0), (-1, -1), 0.5, colors.grey),

            # Alternating row colors
            ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, colors.HexColor('#f0f0f0')])
        ]))
        if show_selling:
            # Total is the number to count against
            table.setStyle(TableStyle([('FONTNAME', (4, 1), (4, -1), 'Helvetica-Bold')]))

        elements.append(table)
        elements.append(Spacer(1, 0.2*inch))

        # Page break after each category except the last
        if page_break_per_category and cat_idx < len(categories) - 1:
            elements.append(PageBreak())

    return elements

def new_pdf_document(buffer, page_size):
    """Letter/A4 document with the standard audit margins"""
    return SimpleDocTemplate(
        buffer,
        pagesize=letter if page_size == 'letter' else A4,
        topMargin=0.5*inch,
        bottomMargin=0.75*inch,
        leftMargin=0.5*inch,
        rightMargin=0.5*inch
    )

def generate_audit_pdf(df, selected_categories, selected_brands, page_size='letter', show_selling=False):
    """
    Generate a professional audit PDF with checkboxes and signature lines

    Args:
        df: Filtered audit dataframe
        selected_categories: List of selected category names
        selected_brands: List of selected brand names
        page_size: 'letter' or 'a4'
        show_selling: Add a Selling Qty column

    Returns:
        BytesIO: PDF file buffer
    """
    buffer = io.BytesIO()
    doc = new_pdf_document(buffer, page_size)
    pdf_styles = get_pdf_styles()

    # Container for elements
    elements = []

    # Title
    elements.append(Paragraph("HAVEN DISTRIBUTION INVENTORY AUDIT WORKSHEET", pdf_styles['title']))
    elements.append(Spacer(1, 0.1*inch))

    # Metadata section - condensed on fewer lines
    audit_date = datetime.now().strftime("%B %d, %Y")
    audit_time = datetime.now().strftime("%I:%M %p")

    # Filter info
    category_text = ", ".join(selected_categories) if selected_categories and 'All' not in selected_categories else "All Categories"
    brand_text = ", ".join(selected_brands) if selected_brands and 'All' not in selected_brands else "All Brands"

    # Count unique batches
    unique_batches = df['Distru Batch Number'].nunique()

    # Condensed metadata on 2 lines
    metadata = [
        f"<b>Date:</b> {audit_date} &nbsp;&nbsp;&nbsp; <b>Time:</b> {audit_time} &nbsp;&nbsp;&nbsp; <b>Unique Batches:</b> {unique_batches}",
        f"<b>Categories:</b> {xml_escape(category_text)} &nbsp;&nbsp;&nbsp; <b>Brands:</b> {xml_escape(brand_text)}"
    ]

    for line in metadata:
        elements.append(Paragraph(line, pdf_styles['header']))

    elements.append(Spacer(1, 0.15*inch))

    elements.extend(build_category_tables(df, pdf_styles, show_selling=show_selling))

    # Signature section on last page
    elements.append(Spacer(1, 0.3*inch))

    signature_table_data = [
        ['Audited By:', '_' * 40, 'Date:', '_' * 20],
        ['', '', '', ''],
        ['Verified By:', '_' * 40, 'Date:', '_' * 20]
    ]

    sig_table = Table(signature_table_data, colWidths=[1*inch, 3*inch, 0.7*inch, 1.8*inch])
    sig_table.setStyle(TableStyle([
        ('FONTNAME', (0, 0), (-1, -1), 'Helvetica'),
        ('FONTSIZE', (0, 0), (-1, -1), 10),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 8),
    ]))

    elements.append(sig_table)

    # Build PDF
    doc.build(elements)

    # Reset buffer position
    buffer.seek(0)
    return buffer

def generate_ticket_audit_pdf(tickets, page_size='letter', show_selling=False, one_per_page=False):
    """
    Generate a ticket audit PDF with no signature lines.
    
    Tickets print back to back so several fit on a page; each ticket is kept
    together and only splits if it is longer than a page.
    
    Args:
        tickets: List of dicts with 'ref', 'comments', 'terms', 'df'
        page_size: 'letter' or 'a4'
        show_selling: Add a Selling Qty column
        one_per_page: Start every ticket on a new page instead
        
    Returns:
        BytesIO: PDF file buffer
    """
    buffer = io.BytesIO()
    doc = new_pdf_document(buffer, page_size)
    pdf_styles = get_pdf_styles()
    base = pdf_styles['base']
    
    audit_date = datetime.now().strftime("%B %d, %Y")
    audit_time = datetime.now().strftime("%I:%M %p")
    
    ticket_heading = ParagraphStyle('TicketHeading', parent=base['Heading2'], spaceBefore=0, spaceAfter=2)
    # Room for the comments box, whose border padding reaches up into the space above it
    ticket_meta = ParagraphStyle('TicketMeta', parent=pdf_styles['header'], spaceAfter=12)
    
    def page_header():
        return [
            Paragraph("HAVEN DISTRIBUTION TICKET AUDIT WORKSHEET", pdf_styles['title']),
            Paragraph(
                f"<b>Date:</b> {audit_date} &nbsp;&nbsp;&nbsp; <b>Time:</b> {audit_time} "
                f"&nbsp;&nbsp;&nbsp; <b>Tickets:</b> {len(tickets)}",
                pdf_styles['header']
            ),
        ]
    
    elements = page_header()
    for idx, ticket in enumerate(tickets):
        if idx > 0:
            if one_per_page:
                elements.append(PageBreak())
                elements.extend(page_header())
            else:
                elements.append(HRFlowable(width='100%', thickness=1, color=colors.HexColor('#1f77b4'),
                                           spaceBefore=2, spaceAfter=10))
        
        ticket_df = ticket['df']
        heading = f"Ticket #{ticket['ref']}" if ticket['ref'] else f"Ticket {ticket['number']}"
        
        block = [
            Paragraph(f"<b>{xml_escape(heading)}</b>", ticket_heading),
            Paragraph(
                f"<b>Search:</b> {xml_escape(', '.join(ticket['terms']))} "
                f"&nbsp;&nbsp;&nbsp; <b>Unique Batches:</b> {count_real_batches(ticket_df)}",
                ticket_meta
            ),
        ]
        
        if ticket['comments']:
            comment_html = xml_escape(ticket['comments']).replace('\n', '<br/>')
            block.append(Paragraph(f"<b>Ticket Comments:</b><br/>{comment_html}", pdf_styles['comments']))
        
        block.extend(build_category_tables(
            ticket_df, pdf_styles, show_selling=show_selling,
            page_break_per_category=False, category_heading='Heading4'
        ))
        
        # Keep a ticket on one page when it fits
        elements.append(KeepTogether(block))
    
    doc.build(elements)
    buffer.seek(0)
    return buffer

# ============================================================================
# MAIN APPLICATION
# ============================================================================

def render_full_audit(audit_df):
    """Brand/category audit builder (the original v2.0 workflow)"""
    # ========================================================================
    # REPORT BUILDER SECTION
    # ========================================================================
    
    st.markdown("---")
    st.header("🔨 Build Your Audit Report")
    st.markdown("Configure your report by selecting categories and brands to include in the physical audit.")
    
    # Create two columns for the builder
    col1, col2 = st.columns(2)
    
    with col1:
        st.subheader("📂 Step 2: Select Categories")
        
        # Quick select buttons
        select_col1, select_col2 = st.columns(2)
        with select_col1:
            if st.button("✅ Select All Categories", use_container_width=True):
                st.session_state.selected_categories = ['All']
        with select_col2:
            if st.button("❌ Clear Categories", use_container_width=True):
                st.session_state.selected_categories = []
        
        # Category selector
        category_options = ['All'] + st.session_state.categories
        
        if 'selected_categories' not in st.session_state:
            st.session_state.selected_categories = ['All']
        
        selected_categories = st.multiselect(
            "Categories to include:",
            options=category_options,
            default=st.session_state.selected_categories,
            help="Select specific categories or 'All' for complete inventory audit",
            key="category_selector"
        )
        
        # Update session state
        st.session_state.selected_categories = selected_categories
        
        # Show count
        if selected_categories and 'All' not in selected_categories:
            cat_filtered = audit_df[audit_df['Category'].isin(selected_categories)]
            st.info(f"📦 {len(cat_filtered):,} items in selected categories")
    
    with col2:
        st.subheader("🏷️ Step 3: Select Brands")
        
        # Quick select buttons
        select_col3, select_col4 = st.columns(2)
        with select_col3:
            if st.button("✅ Select All Brands", use_container_width=True):
                st.session_state.selected_brands = ['All']
        with select_col4:
            if st.button("❌ Clear Brands", use_container_width=True):
                st.session_state.selected_brands = []
        
        # Brand selector
        brand_options = ['All'] + st.session_state.brands
        
        if 'selected_brands' not in st.session_state:
            st.session_state.selected_brands = ['All']
        
        selected_brands = st.multiselect(
            "Brands to include:",
            options=brand_options,
            default=st.session_state.selected_brands,
            help="Select specific brands or 'All' for all brands",
            key="brand_selector"
        )
        
        # Update session state
        st.session_state.selected_brands = selected_brands
        
        # Show count
        if selected_brands and 'All' not in selected_brands:
            brand_filtered = audit_df[audit_df['Brand'].isin(selected_brands)]
            st.info(f"🏷️ {len(brand_filtered):,} items from selected brands")
    
    # Apply filters
    filtered_df = audit_df.copy()
    
    if selected_categories and 'All' not in selected_categories:
        filtered_df = filtered_df[filtered_df['Category'].isin(selected_categories)]
    
    if selected_brands and 'All' not in selected_brands:
        filtered_df = filtered_df[filtered_df['Brand'].isin(selected_brands)]
    
    # ========================================================================
    # PREVIEW & GENERATE SECTION
    # ========================================================================
    
    st.markdown("---")
    st.header("📄 Step 4: Preview & Generate Report")
    
    if filtered_df.empty:
        st.warning("⚠️ No items match your selection. Please adjust your filters.")
        return
    
    show_selling = has_selling_packages(audit_df)
    
    # Summary metrics
    st.subheader("📊 Report Summary")
    metric_cols = st.columns(6 if show_selling else 4)
    metric_col1, metric_col2, metric_col3, metric_col4 = metric_cols[:4]
    
    with metric_col1:
        st.metric("Total Items", f"{len(filtered_df):,}")
    with metric_col2:
        st.metric("Unique Batches", f"{filtered_df['Distru Batch Number'].nunique():,}")
    with metric_col3:
        st.metric("Categories", f"{filtered_df['Category'].nunique():,}")
    with metric_col4:
        total_qty = filtered_df['System_Qty'].sum()
        st.metric("Active Qty" if show_selling else "System Qty", f"{int(total_qty):,}")
    if show_selling:
        with metric_cols[4]:
            st.metric("Selling Qty", f"{int(filtered_df['Selling_Qty'].sum()):,}")
        with metric_cols[5]:
            st.metric("Total Qty", f"{int(filtered_df['System_Qty'].sum() + filtered_df['Selling_Qty'].sum()):,}",
                      help="Active + Selling: selling units are still on the shelf until pulled")
    
    # Preview by category
    st.subheader("🔍 Report Preview")
    
    categories_in_report = sorted(filtered_df['Category'].unique())
    
    for category in categories_in_report:
        cat_data = filtered_df[filtered_df['Category'] == category]
        cat_qty = cat_data['System_Qty'].sum() + cat_data['Selling_Qty'].sum()
        
        with st.expander(f"📦 {category} ({len(cat_data):,} items, {int(cat_qty):,} units)", expanded=False):
            # Show top 10 items - match PDF columns
            display_cols = ['Distru Product', 'Distru Batch Number', 'System_Qty']
            display_names = ['Product', 'Batch #', 'System Qty']
            if show_selling:
                display_cols.append('Selling_Qty')
                display_names = ['Product', 'Batch #', 'Active Qty', 'Selling Qty']
            display_df = cat_data[display_cols].head(10).copy()
            display_df.columns = display_names
            if show_selling:
                display_df['Total Qty'] = display_df['Active Qty'] + display_df['Selling Qty']
            
            st.dataframe(
                display_df,
                use_container_width=True,
                hide_index=True
            )
            if len(cat_data) > 10:
                st.info(f"Showing first 10 of {len(cat_data):,} items. Full list will be in PDF.")
    
    # ========================================================================
    # PDF GENERATION
    # ========================================================================
    
    st.markdown("---")
    st.subheader("📥 Step 5: Download Audit Report")
    
    col_pdf1, col_pdf2 = st.columns([2, 1])
    
    with col_pdf1:
        st.markdown("""
        **Your audit report includes:**
        - ✅ Header with date, time, unique batch count, and filter details
        - ✅ Grouped by category for easy organization
        - ✅ Product names and batch numbers (with text wrapping)
        - ✅ System quantity column for expected counts
        - ✅ Physical count column for manual entry during audit
        - ✅ Signature lines for auditor and verifier
        """)
    
    with col_pdf2:
        page_size = st.radio(
            "Paper Size:",
            options=['letter', 'a4'],
            index=0,
            format_func=lambda x: 'Letter (8.5" x 11")' if x == 'letter' else 'A4'
        )
    
    # Generate PDF button
    if st.button("🎯 Generate Audit PDF", type="primary", use_container_width=True):
        with st.spinner("📄 Generating your audit report..."):
            try:
                # Generate PDF
                pdf_buffer = generate_audit_pdf(
                    filtered_df,
                    selected_categories,
                    selected_brands,
                    page_size,
                    show_selling=show_selling
                )
                
                # Create filename
                timestamp = datetime.now().strftime('%Y%m%d_%H%M')
                category_part = "_".join(selected_categories[:2]) if selected_categories and 'All' not in selected_categories else "All"
                filename = f"DC_Audit_{category_part}_{timestamp}.pdf"
                
                # Download button
                st.success("✅ Report generated successfully!")
                st.download_button(
                    label="📥 Download Audit Report PDF",
                    data=pdf_buffer,
                    file_name=filename,
                    mime="application/pdf",
                    use_container_width=True
                )
                
            except Exception as e:
                st.error(f"❌ Error generating PDF: {str(e)}")
                st.exception(e)
    
    # ========================================================================
    # DEBUG TAB (Optional)
    # ========================================================================
    
    with st.expander("🔍 Debug Information"):
        st.write("**Data Statistics:**")
        st.write(f"- Total packages loaded: {len(audit_df):,}")
        st.write(f"- After filtering: {len(filtered_df):,}")
        st.write(f"- Available categories: {len(st.session_state.categories)}")
        st.write(f"- Available brands: {len(st.session_state.brands)}")
        
        st.write("\n**Sample Data:**")
        st.dataframe(filtered_df.head(10), use_container_width=True)

def default_include_flags(matched_df, term_masks, include_zero):
    """
    Decide which matched rows start ticked.

    Rows with stock are ticked. Zero-qty rows are always listed (so they can
    still be verified) but start unticked, unless the box to print all zeros is
    on or the row came from a search term that found nothing but zeros.
    """
    is_zero = (matched_df['System_Qty'] == 0) & (matched_df['Selling_Qty'] == 0)
    if include_zero:
        return pd.Series(True, index=matched_df.index)

    wanted_zero = pd.Series(False, index=matched_df.index)
    for mask in term_masks.values():
        term_rows = mask[mask].index.intersection(matched_df.index)
        if len(term_rows) and is_zero.loc[term_rows].all():
            wanted_zero.loc[term_rows] = True

    return ~is_zero | wanted_zero

def render_ticket_card(audit_df, products_df, ticket_id, number, show_selling, can_remove):
    """
    One ticket box: ticket #, comments, SKU search and matched rows

    Returns:
        dict for the PDF (ref, comments, terms, df) or None if nothing selected
    """
    key = f"ticket_{ticket_id}"

    with st.container(border=True):
        head_col, remove_col = st.columns([5, 1])
        with head_col:
            st.subheader(f"🎫 Ticket {number}")
        with remove_col:
            if can_remove and st.button("🗑️ Remove", key=f"{key}_remove", use_container_width=True):
                st.session_state.ticket_ids.remove(ticket_id)
                st.rerun()

        info_col, search_col = st.columns(2)

        with info_col:
            ref_col, pull_col = st.columns([3, 2], vertical_alignment="bottom")
            with ref_col:
                ticket_ref = st.text_input(
                    "Ticket # or Freshdesk link (optional):",
                    help="Printed on the audit sheet header",
                    key=f"{key}_ref"
                )
            with pull_col:
                fd_ready = freshdesk_config() is not None
                st.button(
                    "⬇️ Pull from Freshdesk",
                    key=f"{key}_pull",
                    on_click=pull_freshdesk_ticket,
                    args=(key,),
                    disabled=not fd_ready,
                    help="Fills the comments and SKU search from the Freshdesk ticket" if fd_ready
                         else "Add your Freshdesk API key to .streamlit/secrets.toml to turn this on",
                    use_container_width=True
                )
            
            fd_status = st.session_state.pop(f"{key}_fd_status", None)
            if fd_status:
                level, message = fd_status
                {'success': st.success, 'warning': st.warning, 'error': st.error}[level](message)
            comments = st.text_area(
                "Ticket comments:",
                placeholder="Paste the ticket issue here",
                help="Printed on the audit sheet above the items",
                height=140,
                key=f"{key}_comments"
            )

        with search_col:
            raw_search = st.text_area(
                "SKU search (one per line or comma-separated):",
                placeholder="Almora Wedding Cake\nWVY-368-BUD-1\n1A4060300009F2A000172825",
                help="Matches product name, batch number, or package label. "
                     "Every word must appear, in any order (e.g. 'wedding cake almora').",
                height=140,
                key=f"{key}_search"
            )
            include_zero = st.checkbox(
                "Print all zero-quantity items",
                value=False,
                help="Zero-qty items are always listed below but start unticked, "
                     "unless a search finds only zero-qty items. Tick this to print them all.",
                key=f"{key}_include_zero"
            )

        terms = parse_search_terms(raw_search)
        if not terms:
            st.caption("🔍 Enter one or more SKUs to find items for this ticket.")
            return None

        matched_df, term_masks = search_audit_items(audit_df, terms)

        matched_df = matched_df.sort_values(['Category', 'Distru Product', 'Distru Batch Number'])
        include_flags = default_include_flags(matched_df, term_masks, include_zero)
        is_zero = (matched_df['System_Qty'] == 0) & (matched_df['Selling_Qty'] == 0)
        zero_unticked = int((is_zero & ~include_flags).sum())
        if zero_unticked:
            st.caption(f"ℹ️ {zero_unticked} zero-qty item(s) listed but unticked. "
                       "Tick any you need to verify.")
        extra_frames, extra_flags = [], []

        # SKUs with no packages in the export still go on the sheet so they can be counted.
        # The product list (if uploaded) tells real sold-out products apart from typos.
        no_match = [t for t, m in term_masks.items() if not m.any()]
        if no_match and products_df is not None:
            product_rows, product_flags, found = product_list_rows(products_df, no_match)
            if found:
                st.info(f"ℹ️ No packages in this export for: **{', '.join(found)}**. "
                        "Found in the product list and added with 0 qty so you can count it.")
                extra_frames.append(product_rows)
                extra_flags.append(product_flags)
                unticked = int((~product_flags).sum())
                if unticked:
                    st.caption(f"ℹ️ {unticked} other product-list match(es) listed but unticked.")
            no_match = [t for t in no_match if t not in found]

        if no_match:
            known_names = (products_df['Name'] if products_df is not None
                           else audit_df['Distru Product']).dropna().unique()
            where = ("in this export or the product list" if products_df is not None
                     else "in this export")
            lines = [f"⚠️ Not found {where}: **{', '.join(no_match)}** "
                     "(tag older than 180 days or inactive?). "
                     "Added to the sheet as *Not in export* so you can still count it "
                     "(untick if it was a typo)."]
            for term in no_match:
                suggestions = suggest_products(known_names, term)
                if suggestions:
                    lines.append(f"Closest products for '{term}': {'; '.join(suggestions)}")
            st.warning("\n\n".join(lines))

            missing_df = not_in_export_rows(no_match)
            extra_frames.append(missing_df)
            extra_flags.append(pd.Series(True, index=missing_df.index))

        if extra_frames:
            frames = ([matched_df] if not matched_df.empty else []) + extra_frames
            flags = ([include_flags] if not matched_df.empty else []) + extra_flags
            matched_df = pd.concat(frames)
            include_flags = pd.concat(flags)


        # Let the user untick anything the search caught that isn't part of this ticket
        qty_cols = ['System_Qty', 'Selling_Qty'] if show_selling else ['System_Qty']
        info_cols = ['Category', 'Distru Product']
        if products_df is not None:
            info_cols.append('SKU')
        info_cols.append('Distru Batch Number')
        # Fresh index: package rows and placeholder rows have mixed index types
        editor_df = matched_df[info_cols + qty_cols].reset_index(drop=True)
        editor_df.insert(0, 'Include', include_flags.values)
        if show_selling:
            editor_df['Total_Qty'] = editor_df['System_Qty'] + editor_df['Selling_Qty']
            qty_cols = qty_cols + ['Total_Qty']

        # Key on the search so ticks from an old search never land on the wrong rows
        editor_key = f"{key}_editor_{hash((tuple(terms), include_zero, products_df is not None))}"
        edited_df = st.data_editor(
            editor_df,
            column_config={
                'Include': st.column_config.CheckboxColumn('Include', width='small'),
                'Distru Product': 'Product',
                'Distru Batch Number': 'Batch #',
                'System_Qty': st.column_config.NumberColumn(
                    'Active Qty' if show_selling else 'System Qty', format='%d'),
                'Selling_Qty': st.column_config.NumberColumn('Selling Qty', format='%d'),
                'Total_Qty': st.column_config.NumberColumn(
                    'Total Qty', format='%d', help="Active + Selling: what should be on the shelf"),
            },
            disabled=info_cols + qty_cols,
            hide_index=True,
            use_container_width=True,
            key=editor_key
        )

        ticket_df = matched_df[edited_df['Include'].values]

        if ticket_df.empty:
            st.warning("⚠️ No items ticked, so this ticket won't print.")
            return None

        summary = (f"**{len(ticket_df):,}** items · **{count_real_batches(ticket_df):,}** batches · "
                   f"**{int(ticket_df['System_Qty'].sum()):,}** {'active' if show_selling else 'units'}")
        if show_selling:
            summary += (f" · **{int(ticket_df['Selling_Qty'].sum()):,}** selling"
                        f" · **{int(ticket_df['System_Qty'].sum() + ticket_df['Selling_Qty'].sum()):,}** total")
        st.markdown(summary)

        return {
            'number': number,
            'ref': clean_ticket_ref(ticket_ref),
            'comments': comments.strip(),
            'terms': terms,
            'df': ticket_df,
        }

def render_ticket_audit(audit_df, products_df=None):
    """Small, SKU-targeted spot-check audits, one box per ticket"""
    st.header("🎫 Ticket Auditing")
    st.markdown("Add a box for each ticket, search for the SKUs it covers, and print them together.")

    if 'ticket_ids' not in st.session_state:
        st.session_state.ticket_ids = [0]
        st.session_state.next_ticket_id = 1

    show_selling = has_selling_packages(audit_df)
    ticket_ids = st.session_state.ticket_ids

    if products_df is None:
        st.caption("💡 Upload the Distru **Product List CSV** in the sidebar to search by product SKU "
                   "and find sold-out products that have no packages.")
    else:
        audit_df = attach_product_skus(audit_df, products_df)

    tickets = []
    for number, ticket_id in enumerate(list(ticket_ids), start=1):
        ticket = render_ticket_card(audit_df, products_df, ticket_id, number, show_selling,
                                    can_remove=len(ticket_ids) > 1)
        if ticket:
            tickets.append(ticket)

    if st.button("➕ Add another ticket", key="ticket_add"):
        ticket_ids.append(st.session_state.next_ticket_id)
        st.session_state.next_ticket_id += 1
        st.rerun()

    # ========================================================================
    # PDF GENERATION
    # ========================================================================

    st.markdown("---")
    st.subheader("📥 Download Ticket Audit")

    if not tickets:
        st.info("Tick at least one item in a ticket to generate the audit sheet.")
        return


    pdf_col1, pdf_col2 = st.columns([2, 1])
    with pdf_col2:
        page_size = st.radio(
            "Paper Size:",
            options=['letter', 'a4'],
            index=0,
            format_func=lambda x: 'Letter (8.5" x 11")' if x == 'letter' else 'A4',
            key="ticket_page_size"
        )
        one_per_page = st.checkbox(
            "Start each ticket on a new page",
            value=False,
            help="Off: tickets print back to back (a ticket is only split if it's longer than a page)",
            key="ticket_one_per_page"
        )
    
    with pdf_col1:
        if one_per_page:
            st.caption(f"{len(tickets)} ticket(s) will print, each starting on its own page.")
        else:
            st.caption(f"{len(tickets)} ticket(s) will print back to back, as many per page as fit.")

        if st.button("🎯 Generate Ticket Audit PDF", type="primary",
                     use_container_width=True, key="ticket_generate"):
            with st.spinner("📄 Generating your ticket audit..."):
                try:
                    pdf_buffer = generate_ticket_audit_pdf(tickets, page_size, show_selling=show_selling,
                                                           one_per_page=one_per_page)

                    timestamp = datetime.now().strftime('%Y%m%d_%H%M')
                    refs = [re.sub(r'[^A-Za-z0-9-]+', '_', t['ref']).strip('_') for t in tickets]
                    ref_part = "_".join(r for r in refs[:3] if r)
                    filename = f"DC_Ticket_Audit_{ref_part + '_' if ref_part else ''}{timestamp}.pdf"

                    st.success("✅ Ticket audit generated!")
                    st.download_button(
                        label="📥 Download Ticket Audit PDF",
                        data=pdf_buffer,
                        file_name=filename,
                        mime="application/pdf",
                        use_container_width=True,
                        key="ticket_download"
                    )

                except Exception as e:
                    st.error(f"❌ Error generating PDF: {str(e)}")
                    st.exception(e)

def main():
    # Title
    st.title(f"📋 DC Audit Report Generator v{VERSION}")
    st.markdown("Build custom physical audit sheets with PDF export")
    
    # Sidebar - Upload only
    st.sidebar.header("📊 Data Source")
    
    st.sidebar.subheader("📄 Upload Packages CSV")
    uploaded_file = st.sidebar.file_uploader(
        "Upload Packages CSV:",
        type=['csv'],
        help="Upload the packages CSV export from Distru"
    )
    
    fd_config = freshdesk_config()
    if fd_config:
        st.sidebar.caption(f"🔗 Freshdesk connected: {fd_config[0]}")
    else:
        st.sidebar.caption("🔗 Freshdesk not set up (add your API key to `.streamlit/secrets.toml`)")
    
    st.sidebar.subheader("🗂️ Product List CSV (optional)")
    products_file = st.sidebar.file_uploader(
        "Upload Product List CSV:",
        type=['csv'],
        help="Distru products export. Lets Ticket Auditing search by product SKU and "
             "find sold-out products that have no packages.",
        key="products_uploader"
    )
    
    # Initialize session state for workflow
    if 'audit_df' not in st.session_state:
        st.session_state.audit_df = None
    if 'categories' not in st.session_state:
        st.session_state.categories = []
    if 'brands' not in st.session_state:
        st.session_state.brands = []
    
    # Main content
    if not uploaded_file:
        st.info("👈 **Step 1:** Upload a packages CSV file to begin building your audit report")
        
        # Show example format
        with st.expander("📖 Expected CSV Format"):
            st.markdown("""
            Your CSV should contain these columns:
            - **Distru Product**: Full product name (e.g., "Brand - Product Description")
            - **Category**: Product category (e.g., "Vape", "Flower (Indica)", "Gummies")
            - **Distru Batch Number**: Batch identifier
            - **Available Quantity**: Quantity available in package
            
            The brand will be automatically extracted from the product name.
            """)
        return
    
    # Load and process data (re-process when a different file is uploaded)
    if st.session_state.audit_df is None or st.session_state.get('loaded_file_id') != uploaded_file.file_id:
        with st.spinner("🔄 Processing packages data..."):
            packages_df = load_packages_csv(uploaded_file)
            
            if packages_df is None:
                st.error("❌ Failed to load CSV file")
                return
            
            # Validate
            valid, message = validate_required_columns(packages_df)
            if not valid:
                st.error(f"❌ {message}")
                return
            
            # Process
            audit_df = process_packages_to_audit(packages_df)
            st.session_state.audit_df = audit_df
            st.session_state.loaded_file_id = uploaded_file.file_id
            st.session_state.categories = sorted(audit_df['Category'].unique().tolist())
            st.session_state.brands = sorted(audit_df['Brand'].unique().tolist())
    
    audit_df = st.session_state.audit_df
    
    # Optional product list (re-loaded when a different file is uploaded)
    products_df = None
    if products_file:
        if st.session_state.get('products_file_id') != products_file.file_id:
            st.session_state.products_df = load_product_list_csv(products_file)
            st.session_state.products_file_id = products_file.file_id
        products_df = st.session_state.products_df

    if not has_selling_packages(audit_df):
        st.sidebar.warning("⚠️ This export has no packages in **Selling** status, so units waiting "
                           "to be pulled won't be counted. Include Selling status in the Distru "
                           "packages export.")

    # Section switcher
    st.sidebar.markdown("---")
    section = st.sidebar.radio(
        "📂 Section",
        options=["🔨 Full Audit", "🎫 Ticket Auditing"],
        key="section"
    )
    st.sidebar.markdown("---")
    
    # Changelog in sidebar
    with st.sidebar.expander("📋 Version History"):
        st.markdown("""
        **v2.2** (Current)
        - Pull ticket details from Freshdesk
        
        **v2.1**
        - Ticket Auditing section (sidebar): multiple tickets, SKU search, comments
        - Zero-qty items listed but unticked by default
        - Selling Qty + Total Qty (Active + Selling) columns
        - Ticket sheets skip the signature lines
        - Optional Product List CSV: SKU search, sold-out products still print
        - Missing SKUs / tags print as "Not in export"
        - New CSV uploads re-process automatically
        
        **v2.0**
        - Redesigned with "Report Builder" workflow
        - Added PDF generation for physical audits
        - Moved filters to main content area
        - Added audit-friendly formatting
        - Signature lines and metadata
        
        **v1.0** (2025-11-07)
        - Initial CSV processing release
        """)
    
    st.sidebar.markdown("---")
    st.sidebar.markdown(f"**Version {VERSION}**")
    
    if section == "🎫 Ticket Auditing":
        render_ticket_audit(audit_df, products_df)
    else:
        render_full_audit(audit_df)

if __name__ == "__main__":
    main()