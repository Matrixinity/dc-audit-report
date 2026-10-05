# DC Audit Report — Notes

Fork: https://github.com/Matrixinity/dc-audit-report
Local copy: `C:\Users\Distr\Documents\Apps\dc-audit-report`

Streamlit app that turns a Distru **packages CSV** export into printable
physical-audit PDFs (Product / Batch # / quantities / Physical Count).

Pick the section in the **sidebar** under *📂 Section*.

## 🔨 Full Audit (original v2.0 workflow)
- Filter by **Category** and **Brand** (brand = text before ` - ` in the product name).
- Includes zero-quantity items, which is what you want for big audits.
- Each category starts on a new PDF page. Signature lines at the end.

## 🎫 Ticket Auditing (added v2.1)
For quick spot checks on specific SKUs, one box per ticket.
- **➕ Add another ticket** adds a box; **🗑️ Remove** deletes one.
- Each ticket has:
  - **Ticket #** (optional): printed as the page heading and in the filename.
    A leading `#` is fine.
  - **Ticket comments**: paste the whole ticket issue; it prints in a box
    above the items.
  - **SKU search**: one term per line or comma-separated. Matches product name,
    batch number, or package label (Metrc tag). Every word must appear, in any
    order, case-insensitive: `almora wedding cake` finds `Almora - Wedding Cake 3.5g`.
  - **Include** checkbox per row: untick anything the search caught that isn't
    part of the ticket. Changing the search resets the ticks.
- **Zero-quantity items** are always listed so you can still verify them:
  - They start **unticked**, so broad searches don't fill the sheet with zeros.
  - They start **ticked** if a search term found *only* zero-qty items
    (you clearly searched for that SKU).
  - **Print all zero-quantity items** ticks them all for that ticket.
- **SKUs with no packages still print**, so you can count them:
  - Upload the optional **Product List CSV** (Distru products export, the *full* list,
    not available-only) in the sidebar. A searched product that exists in Distru but has
    no packages (e.g. sold out) prints under its real category with 0 qty and Batch #
    *No packages in export*. An exact name or SKU match starts ticked; looser matches are
    listed unticked.
  - With the product list loaded you can also **search by the Distru product SKU**
    (e.g. `850039736230`), and a SKU column shows in the results.
  - Anything still not found prints as **Not in export** (untick it if it was a typo),
    with the closest product names suggested. The packages export only includes tags
    that are active and existed in the last 180 days, so an older or inactive tag from a
    ticket lands here. Search the tag to put it on the sheet, or re-export without that
    filter to get its real batch.
- One PDF for all tickets. Tickets print **back to back, as many per page as fit**
  (about 3 short tickets on Letter). A ticket is never split across pages unless it's
  longer than a page. Tick *Start each ticket on a new page* for the old one-per-page
  layout. No signature lines on ticket sheets.

## Quantities
- **Active Qty** = `Available Quantity` of packages that are *not* in selling status.
- **Selling Qty** = `Quantity` of packages whose `Status` is `selling`.
- **Total Qty** = Active + Selling, printed **bold**. This is the number to count
  against: selling units are still on the shelf until someone pulls them
  (e.g. Distru says 100 active, but 100 more are on selling, so 200 should be there).
  - Selling and Total only appear when the uploaded export has selling packages;
    otherwise sheets show a single *System Qty* column as before.
  - The Distru packages export must **include packages in Selling status**. If it
    doesn't, the sidebar shows a warning.
  - If selling units have already been pulled for an order, the count will come in
    under Total by that amount.
  - Assumption to verify with a real export: selling packages use their full
    `Quantity`, since those units are still physically on hand.
- Rows are grouped by Category + Product + Batch and quantities summed, so a
  batch with 0 in one package and 1 in another shows as 1.
- Uploading a new CSV re-processes it (before v2.1 the first file stuck until refresh).

## Run locally
Double-click **`Run DC Audit.cmd`**, or open **`Open DC Audit.html`** while it's running
(app lives at http://localhost:8501).

Manual:
```bash
cd "C:/Users/Distr/Documents/Apps/dc-audit-report"
.venv/Scripts/streamlit run app.py
```
(`.venv` is git-ignored. `Run DC Audit.cmd` creates it on first run if missing.)
