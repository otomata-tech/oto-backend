## usage — greek companies

identifies a greek entity in the public registers (gemi + vies), no key needed.
- `gr_lookup(query=…)` accepts a **name**, a **gemi number** or a greek **vat number** (ΑΦΜ, with or without the `EL` prefix)
- returns the matching companies (name, gemi number, vat, active/inactive status)
- for a single result: adds the address and the validity of the vat number via vies; a vies outage is named in `vat_check_error`, never read as an invalid number
