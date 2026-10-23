# QuickBooks Online export, as a sample

Invented books in the exact layout of QuickBooks Online's Export data bundle
(the gear → Tools → Export data): a leading blank column on the journal and
the lists, the title rows, every figure as formula text (`=4010.00`),
labels indented three spaces per level, Parent:Child sub-accounts, a parent
with no activity of its own as a bare heading, a totals row and a blank row
after every journal block, a memo-only row and an item at 0.00 with no
account inside an invoice, and the footer that names the basis.

The sample company is **Sutehk Realty Group**, named for @sutehk, whose two
hours in Intuit's developer portal are why this route exists. The real
export the importer was built against is a user's business and is not here.

`Sutehk_Realty_Group_A_R_Aging_Detail_Report.csv` is the company's A/R Aging
Detail report (Reports → A/R Aging Detail → Export to Excel, a download of its
own beside the zip), as the route reads the workbook: the title rows, the
header from the second column, a section per aging bucket, oldest first, with
its "Total for" row of formulas, the TOTAL row, three blank rows and the time
it was run. It agrees with the Journal: two invoices still open (1003 partly
paid), one payment partly applied and one not applied at all, 525.00 in all,
what the Journal leaves on Accounts Receivable (A/R).

`Trial_balance_accrual.csv` is the same Trial Balance run on the accrual
basis, which the Journal can be verified against. The cash-basis reports show
the payments not yet applied as a negative A/R, as QuickBooks Online's do.
