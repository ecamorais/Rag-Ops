# Retrieval evaluation

- Run date: 2026-10-27 | golden set frozen on 2026-10-27 | 33 questions
- sha256 of data/tickets.json (line endings normalised to LF): 77d196040ac7f348ced07cbffede7646f0efb9e93da79183f386cf5b563620a3
- sha256 of the golden set (LF): b97b10372e08b9da3fe96c9a7c294b8d40c7f8f015337d198c1efa4bbcf9ba01
- Index: 800 tickets, embedding model all-MiniLM-L6-v2, similarity threshold 0.3
- Model: stub only; calls to the stub 96, real client created: no, HTTP attempts: 0
- Measures retrieval only, not answer quality. `expected` is a lower bound (unlabelled tickets can also fit a question).

## k = 3

| Group | n | Hit rate | Recall | Capped recall | MRR |
|---|---|---|---|---|---|
| exact id | 3 | 1.00 | 1.00 | 1.00 | 1.00 |
| cluster / topic | 10 | 0.60 | 0.28 | 0.47 | 0.48 |
| vague ticket | 3 | 1.00 | 1.00 | 1.00 | 1.00 |
| near-duplicate pair | 4 | 0.75 | 0.75 | 0.75 | 0.62 |
| with filters | 4 | 1.00 | 1.00 | 1.00 | 0.88 |
| HEADLINE: all types except known-weak | 29 | 0.79 | 0.59 | 0.69 | 0.68 |
| HEADLINE without the extra type | 24 | 0.79 | 0.66 | 0.74 | 0.70 |
| known weak: aggregation / counting | 2 | 0.00 | 0.00 | 0.00 | 0.00 |
| known weak: vocabulary mismatch | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| extra (assistant-drafted, not blind) * | 5 | 0.80 | 0.28 | 0.47 | 0.57 |

\* also_relevant (not in any headline row), A02, A03, A04: found 5 of 7 at k=3.
- G04 (expect_missing ['OPS-9999']): PASS; retrieved [], reported missing ['OPS-9999'].
- filter_must_hold: 15 hits checked in 5 questions, 0 violations.

## k = 6 (app default)

| Group | n | Hit rate | Recall | Capped recall | MRR |
|---|---|---|---|---|---|
| exact id | 3 | 1.00 | 1.00 | 1.00 | 1.00 |
| cluster / topic | 10 | 1.00 | 0.64 | 0.64 | 0.57 |
| vague ticket | 3 | 1.00 | 1.00 | 1.00 | 1.00 |
| near-duplicate pair | 4 | 1.00 | 0.88 | 0.88 | 0.68 |
| with filters | 4 | 1.00 | 1.00 | 1.00 | 0.88 |
| HEADLINE: all types except known-weak | 29 | 0.97 | 0.79 | 0.81 | 0.71 |
| HEADLINE without the extra type | 24 | 1.00 | 0.83 | 0.83 | 0.74 |
| known weak: aggregation / counting | 2 | 0.50 | 0.10 | 0.10 | 0.08 |
| known weak: vocabulary mismatch | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| extra (assistant-drafted, not blind) * | 5 | 0.80 | 0.60 | 0.70 | 0.57 |

\* also_relevant (not in any headline row), A02, A03, A04: found 6 of 7 at k=6.
- G04 (expect_missing ['OPS-9999']): PASS; retrieved [], reported missing ['OPS-9999'].
- filter_must_hold: 30 hits checked in 5 questions, 0 violations.

## k = 10

| Group | n | Hit rate | Recall | Capped recall | MRR |
|---|---|---|---|---|---|
| exact id | 3 | 1.00 | 1.00 | 1.00 | 1.00 |
| cluster / topic | 10 | 1.00 | 0.82 | 0.82 | 0.57 |
| vague ticket | 3 | 1.00 | 1.00 | 1.00 | 1.00 |
| near-duplicate pair | 4 | 1.00 | 1.00 | 1.00 | 0.68 |
| with filters | 4 | 1.00 | 1.00 | 1.00 | 0.88 |
| HEADLINE: all types except known-weak | 29 | 1.00 | 0.89 | 0.90 | 0.72 |
| HEADLINE without the extra type | 24 | 1.00 | 0.93 | 0.93 | 0.74 |
| known weak: aggregation / counting | 2 | 0.50 | 0.50 | 0.50 | 0.08 |
| known weak: vocabulary mismatch | 1 | 1.00 | 1.00 | 1.00 | 1.00 |
| extra (assistant-drafted, not blind) * | 5 | 1.00 | 0.71 | 0.75 | 0.59 |

\* also_relevant (not in any headline row), A02, A03, A04: found 7 of 7 at k=10.
- G04 (expect_missing ['OPS-9999']): PASS; retrieved [], reported missing ['OPS-9999'].
- filter_must_hold: 50 hits checked in 5 questions, 0 violations.

## Per question

Found = expected ids found in the top k / number expected. First hit = rank within the top 10.

| Id | Type | Flags | Question | First hit | Found @3 | Found @6 | Found @10 |
|---|---|---|---|---|---|---|---|
| G01 | exact_id | - | What happened in ticket OPS-1001? | 1 | 1/1 | 1/1 | 1/1 |
| G02 | exact_id | - | Which ticket is about purchase order PO-165700? | 1 | 1/1 | 1/1 | 1/1 |
| G03 | exact_id | - | Find the ticket for invoice INV-323839. | 1 | 1/1 | 1/1 | 1/1 |
| G04 | exact_id | n/a labels | What is the status of ticket OPS-9999? | - | ok | ok | ok |
| G05 | cluster | - | What caused the recurring VPN outages and how were they f... | 1 | 3/6 | 5/6 | 6/6 |
| G06 | cluster | - | Why did dispatch stop at Chennai when the power went out? | 1 | 3/6 | 6/6 | 6/6 |
| G07 | cluster | - | Why does the Bengaluru server room keep losing its cooling? | 4 | 0/5 | 2/5 | 5/5 |
| G08 | cluster | - | Which supplier started charging us more than the agreed r... | 6 | 0/4 | 1/4 | 4/4 |
| G09 | cluster | - | Why could customers not sign in to the portal at the same... | 1 | 2/6 | 3/6 | 3/6 |
| G10 | cluster | - | Why do we keep seeing rodents in the Chennai warehouse? | 2 | 2/3 | 3/3 | 3/3 |
| G11 | cluster | - | Why did TLS handshakes fail on the tracking service? | 1 | 3/5 | 5/5 | 5/5 |
| G26 | cluster | - | Why did the bank refuse our vendor payment batch? | 4 | 0/3 | 2/3 | 2/3 |
| G27 | cluster | - | Why do we keep running out of stock before the replenishm... | 3 | 1/6 | 3/6 | 4/6 |
| G28 | cluster | - | Why are purchase orders waiting for weeks for sign-off? | 6 | 0/5 | 1/5 | 2/5 |
| G12 | vague | - | Someone reported the whole system being sluggish. | 1 | 1/1 | 1/1 | 1/1 |
| G13 | vague | - | Which ticket says the AC is not working in the office? | 1 | 1/1 | 1/1 | 1/1 |
| G14 | vague | - | A customer is asking where their order is and needs help. | 1 | 1/1 | 1/1 | 1/1 |
| G15 | near_duplicate | - | Which tickets report that portal-web-prod-02 is almost ou... | 2 | 2/2 | 2/2 | 2/2 |
| G16 | near_duplicate | - | Was the housekeeping at HQ floor 4 skipped? | 1 | 2/2 | 2/2 | 2/2 |
| G17 | near_duplicate | vocab_gap | Why can nobody use the elevator at head office? | 5 | 0/2 | 1/2 | 2/2 |
| G18 | near_duplicate | - | Has the refund to Meridian Retail been paid yet? | 1 | 2/2 | 2/2 | 2/2 |
| G19 | filter | filter | Why did the VPN gateway stop accepting connections? | 1 | 3/3 | 3/3 | 3/3 |
| G20 | filter | filter | Cooling failure in the server room | 1 | 1/1 | 1/1 | 1/1 |
| G21 | filter | filter | Outbound dispatch halted at Chennai after a power outage | 1 | 3/3 | 3/3 | 3/3 |
| G22 | filter | filter | Temperature excursion or cold chain alarm at Bengaluru | 2 | 2/2 | 2/2 | 2/2 |
| G23 | known_weak_aggregation | weak | Which vendor has the most delayed delivery tickets? | - | 0/3 | 0/3 | 0/3 |
| G24 | known_weak_aggregation | weak | How many tickets are there about cooling failures in the ... | 6 | 0/5 | 1/5 | 5/5 |
| G25 | known_weak_vocabulary | weak | Who could not log in to the HQ server room? | 1 | 3/3 | 3/3 | 3/3 |
| A01 | extra | - | What problems have we had with NorthStar Components? | 1 | 3/12 | 6/12 | 9/12 |
| A02 | extra | - | Why were the stock counts at Hyderabad so far off? | 1 | 1/4 | 4/4 | 4/4 |
| A03 | extra | filter | Which customers complained about late or repeated deliver... | 3 | 1/4 | 2/4 | 3/4 |
| A04 | extra | - | anyone know why coastal beverages keeps asking for sla cr... | 2 | 2/3 | 3/3 | 3/3 |
| A05 | extra | - | Which incidents were caused by something expiring? | 10 | 0/14 | 0/14 | 1/14 |

## Misses at k = 6

- k=3: no hit for ['G07', 'G08', 'G26', 'G28', 'G17', 'G23', 'G24', 'A05']
- k=10: no hit for ['G23']

### G23 (known_weak_aggregation, known weak): Which vendor has the most delayed delivery tickets?
- expected ['OPS-1468', 'OPS-1696', 'OPS-1763']; first hit within the top 10: rank none
- retrieved at k=6: 1. OPS-1240 (0.58) Supplier delay; 2. OPS-1292 (0.531) Apex Packaging Ltd delivery late against PO-158654; 3. OPS-1472 (0.522) Gateway Fasteners on-time delivery at 39% below target; 4. OPS-1796 (0.518) Contract with Gateway Fasteners expires next month; 5. OPS-1399 (0.512) Repeated delivery complaints from Pinnacle Wholesale on Konkan Carriers; 6. OPS-1285 (0.507) Shipment SHP-3237232 delayed by Trident Freight

### A05 (extra (assistant-drafted, not blind)): Which incidents were caused by something expiring?
- expected ['OPS-1631', 'OPS-1658', 'OPS-1659', 'OPS-1682', 'OPS-1731', 'OPS-1106', 'OPS-1107', 'OPS-1108', 'OPS-1109', 'OPS-1110', 'OPS-1112', 'OPS-1542', 'OPS-1552', 'OPS-1570']; first hit within the top 10: rank 10
- retrieved at k=6: 1. OPS-1274 (0.469) Near-expiry batch of SKU-28350 found at Chennai; 2. OPS-1037 (0.459) Near-expiry batch of SKU-29192 found at Chennai; 3. OPS-1239 (0.456) Expiring stock of SKU-90318 still in pick faces; 4. OPS-1645 (0.456) Near-expiry batch of SKU-95397 found at Chennai; 5. OPS-1644 (0.447) Expiring stock of SKU-85197 still in pick faces; 6. OPS-1601 (0.443) Near-expiry batch of SKU-23935 found at Chennai
- retrieved ids that are listed unlabelled competitors: ['OPS-1274', 'OPS-1037', 'OPS-1239', 'OPS-1645', 'OPS-1644', 'OPS-1601']

## Incomplete at k = 6 (hit, but not every expected id found)

- G05 5/6, not found: ['OPS-1269']
- G07 2/5, not found: ['OPS-1460', 'OPS-1555', 'OPS-1560']
- G08 1/4, not found: ['OPS-1017', 'OPS-1049', 'OPS-1066']
- G09 3/6, not found: ['OPS-1107', 'OPS-1109', 'OPS-1110']
- G26 2/3, not found: ['OPS-1340']
- G27 3/6, not found: ['OPS-1762', 'OPS-1765', 'OPS-1795']
- G28 1/5, not found: ['OPS-1330', 'OPS-1353', 'OPS-1406', 'OPS-1422']
- G17 1/2, not found: ['OPS-1546']
- G24 1/5, not found: ['OPS-1460', 'OPS-1482', 'OPS-1555', 'OPS-1560']
- A01 6/12, not found: ['OPS-1197', 'OPS-1232', 'OPS-1553', 'OPS-1563', 'OPS-1594', 'OPS-1649']
- A03 2/4, not found: ['OPS-1420', 'OPS-1488']
