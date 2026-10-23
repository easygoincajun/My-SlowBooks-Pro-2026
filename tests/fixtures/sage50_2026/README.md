# Sage 50 US 2026 chart export

This fixture is an excerpt of an actual CSV exported on 2026-10-13 from
Sage 50 US 2026.1 (Peachw.exe file version 2026.1.00.0207), using the
built-in **Bellwether Garden Supply** demonstration company. The sample
opens in Sage 50 Quantum Accounting from the installed Accountant edition.
It contains sample chart data, not a customer company or personal records.

Export path in Sage: File > Select Import/Export > General Ledger >
Chart of Accounts List > Export. On Options, enable Include Headings.
The export used the default selected fields.

Reduction: retain the original header and the first row for each distinct
Account Type, preserving all fields and values. Only line endings were
normalised to LF. The original contains 156 account rows; this excerpt
contains 16 rows. The raw local export SHA-256 is:

`bb0601dd09ad3e7fcfe4d99863447ed5cca98970b67bd96daea368aa44b61d7f`

Numeric mappings are cross-checked against Sage's
[Chart of Accounts import/export field reference](https://us-kb.sage.com/portal/app/portlets/results/botviewsolution.jsp?hypermediatext=null&solutionid=221924850013887).
Codes 3 and 11 (retainage) are documented but absent from this sample;
their unit-test rows are explicitly synthetic. Unknown and reserved codes
must still be rejected.

This fixture verifies the chart dialect only. The integration test's
ledger is synthetic and does not claim to verify a Sage ledger export.
