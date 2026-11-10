# Townhome lots: rough per-lot backtest, 2026-10-28

Townhome lots sold as vacant 2024-10-21 to 2026-10-21 and now built (CAMA TOWNHOUSE), one row per deed. Price per lot = deed price / parcels on the deed. Model = one townhome unit's residual before site work (nearby townhome comps as of the sale, house-model costs, loan land = price per lot). Rough: no zoning cap, absorption or site work, so it overstates what a lot is worth.

Deeds 26; valued 26.

| group | sales | complete | median RLV/paid | p25 | p75 | median abs error | mean abs error |
|---|---|---|---|---|---|---|---|
| all | 26 | 0 | 0.81 | 0.65 | 1.41 | 41% | 89% |
| comps <250 $/sf | 14 | 0 | 0.65 | 0.56 | 0.99 | 42% | 45% |
| comps 250-350 $/sf | 11 | 0 | 1.31 | 0.81 | 1.91 | 34% | 142% |
| comps 350-500 $/sf | 1 | 0 | 2.28 | 2.28 | 2.28 | 128% | 128% |
| comps 500-750 $/sf | 0 | 0 | - | - | - | - | - |
| comps 750+ $/sf | 0 | 0 | - | - | - | - | - |

| deed | first pin | lots | paid per lot | unit residual | ratio | comps $/sf | blank |
|---|---|---|---|---|---|---|---|
| 39296-399 | 11320323 | 24 | $130,000 | $53,831 | 0.41 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39301-743 | 11320340 | 21 | $135,000 | $59,505 | 0.44 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39189-597 | 09707635 | 4 | $75,000 | $34,998 | 0.47 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39487-593 | 11502197 | 2 | $82,500 | $45,832 | 0.56 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39240-773 | 06104333 | 5 | $107,200 | $59,634 | 0.56 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39183-160 | 06104322 | 5 | $107,200 | $59,634 | 0.56 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39395-996 | 14308181 | 4 | $69,875 | $45,229 | 0.65 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39371-514 | 13516322 | 6 | $76,917 | $49,836 | 0.65 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39898-909 | 19920712 | 6 | $157,500 | $104,583 | 0.66 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39284-192 | 01903150 | 4 | $135,000 | $91,130 | 0.68 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39312-525 | 01905146 | 6 | $121,833 | $91,130 | 0.75 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39370-346 | 16314222 | 10 | $242,350 | $187,818 | 0.77 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39272-546 | 16902401 | 6 | $175,000 | $141,062 | 0.81 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39232-186 | 16902407 | 7 | $175,000 | $143,415 | 0.82 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39813-539 | 20143205 | 66 | $72,091 | $76,707 | 1.06 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39443-841 | 15715427 | 10 | $141,300 | $163,613 | 1.16 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39291-032 | 10918602 | 82 | $31,646 | $40,932 | 1.29 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39170-549 | 06903951 | 4 | $149,500 | $195,673 | 1.31 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39405-905 | 03107440 | 2 | $42,500 | $59,441 | 1.40 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39311-932 | 06505414 | 2 | $117,500 | $166,016 | 1.41 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39323-847 | 07113151 | 2 | $122,500 | $184,026 | 1.50 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39371-541 | 10324224 | 102 | $59,642 | $132,883 | 2.23 | <250 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39516-716 | 06109730 | 3 | $101,667 | $231,369 | 2.28 | 350-500 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39340-256 | 03904811 | 4 | $67,500 | $156,649 | 2.32 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39605-476 | 01715661 | 5 | $40,000 | $150,818 | 3.77 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
| 39161-338 | 08901708 | 34 | $16,176 | $165,569 | 10.24 | 250-350 | financing.construction_rate_pct, financing.loan_closing_pct |
