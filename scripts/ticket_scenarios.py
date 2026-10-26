"""Hand-written scenario templates for the Operations Tickets dataset.

Pure data plus a validate() helper. generate_tickets.py does the filling.

Template fields
  id          unique scenario id (INF-01, LOG-02, ...)
  category    one of CATEGORIES
  team        default owning team (from CATEGORY_TEAM)
  titles      two title variants, may contain {slots}
  description core sentences (always used); extra holds optional sentences
  extra       optional detail sentences (generator adds 1-2 when the pool has
              3 or more, otherwise 0 to all of them)
  cause       root cause sentence for resolution_notes
  fix         resolution sentence(s) for resolution_notes
  priority    key into PRIORITY_PROFILES
  anchor      slots held fixed across a single-category cluster (the recurring entity)
  link        key into CROSS_CATEGORY_LINKS if the template is one side of a
              cross-category cluster, else None

Slot pools (SLOTS)
  list                      pick one item
  ("range", lo, hi)         random int, rendered with thousands separators
  ("pattern", fmt, lo, hi)  fmt.format(random int in [lo, hi])
"""

import itertools
import re

CATEGORIES = [
    "Infrastructure",
    "Logistics",
    "Inventory",
    "Vendor Management",
    "Customer Escalation",
    "Access & Security",
    "Facilities",
    "Billing & Finance",
]

CATEGORY_TEAM = {
    "Infrastructure": "Platform Ops",
    "Logistics": "Warehouse Ops",
    "Inventory": "Warehouse Ops",
    "Vendor Management": "Procurement",
    "Customer Escalation": "Customer Ops",
    "Access & Security": "IT Security",
    "Facilities": "Facilities",
    "Billing & Finance": "Finance Ops",
}

# Weights for (P1-Critical, P2-High, P3-Medium, P4-Low)
PRIORITY_PROFILES = {
    "crit": (35, 45, 15, 5),
    "high": (5, 35, 45, 15),
    "med": (1, 14, 55, 30),
    "low": (0, 4, 36, 60),
}

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Arjun", "Rohan", "Karthik", "Siddharth", "Rahul",
    "Vikram", "Nikhil", "Amit", "Suresh", "Manoj", "Deepak", "Harish", "Naveen",
    "Prakash", "Ravi", "Sanjay", "Varun", "Ananya", "Priya", "Kavya", "Meera",
    "Divya", "Neha", "Pooja", "Sneha", "Swati", "Lakshmi", "Anjali", "Deepika",
    "Shreya", "Ishita", "Radhika", "Nandini", "Pallavi", "Rekha", "Sunita", "Tanvi",
]

LAST_NAMES = [
    "Sharma", "Iyer", "Reddy", "Nair", "Patil", "Kulkarni", "Deshmukh", "Joshi",
    "Mehta", "Gupta", "Rao", "Menon", "Pillai", "Naidu", "Chowdhury", "Banerjee",
    "Mukherjee", "Das", "Singh", "Kapoor", "Malhotra", "Bhat", "Hegde", "Shetty",
    "Krishnan", "Subramanian", "Venkatesh", "Agarwal", "Jain", "Shah", "Desai",
    "Thakur", "Chauhan", "Verma", "Mishra", "Pandey", "Srinivasan", "Raman",
    "Gowda", "Bose",
]

SLOTS = {
    "site": ["Pune", "Bengaluru", "Chennai", "Hyderabad"],
    "server": [
        "ord-api-prod-01", "ord-api-prod-02", "inv-svc-prod-03", "wms-app-prod-01",
        "wms-app-prod-02", "track-svc-prod-01", "portal-web-prod-02", "erp-app-prod-01",
    ],
    "db_host": [
        "pg-orders-primary", "pg-orders-replica-02", "pg-inventory-01",
        "mysql-wms-01", "pg-billing-01", "pg-tracking-01",
    ],
    "service": [
        "order API", "inventory service", "tracking service", "payment gateway",
        "WMS API", "customer portal",
    ],
    "app": ["WMS", "TMS", "ERP", "vendor portal", "customer portal", "ticketing portal"],
    "vendor": [
        "Apex Packaging Ltd", "Kaveri Pallets Pvt Ltd", "NorthStar Components",
        "Sahyadri Cartons", "Deccan Industrial Supplies", "BrightWave Labels",
        "Orion Cold Systems", "Malabar Plastics", "Lotus Stretch Wrap",
        "Gateway Fasteners",
    ],
    "carrier": [
        "BlueLine Logistics", "SwiftHaul", "Trident Freight", "RoadRunner Express",
        "Konkan Carriers",
    ],
    "customer": [
        "Meridian Retail", "Orchid Pharma", "Sundaram Foods", "Vertex Electronics",
        "Harbor Home Stores", "Lumen Apparel", "GreenLeaf Organics", "Zenith Auto Parts",
        "Pinnacle Wholesale", "Coastal Beverages",
    ],
    "error_code": [
        "HTTP 503", "HTTP 504", "ERR_CONN_RESET", "E4017", "E2209", "SQLSTATE 57P01",
        "TIMEOUT-408", "ERR-5031",
    ],
    "order_no": ("pattern", "SO-{:07d}", 1000000, 1999999),
    "po_no": ("pattern", "PO-{:06d}", 100000, 199999),
    "invoice_no": ("pattern", "INV-{:06d}", 200000, 499999),
    "shipment_no": ("pattern", "SHP-{:07d}", 3000000, 3999999),
    "sku": ("pattern", "SKU-{:05d}", 10000, 99999),
    "device": ("pattern", "HHS-{:03d}", 100, 499),
    "vpn_gateway": ["vpn-gw-blr-01", "vpn-gw-pune-02", "vpn-gw-hyd-01", "vpn-gw-maa-01"],
    "dock": ["Dock 2", "Dock 4", "Dock 7", "Dock 9"],
    "zone": ["HQ main lobby", "HQ floor 4", "HQ server room", "warehouse gate"],
    "qty": ("range", 20, 900),
    "amount": ("range", 15000, 950000),
    "hours": ("range", 2, 36),
    "pct": ("range", 8, 45),
    "user_count": ("range", 12, 240),
    "temp": ("range", 9, 18),
}

# ---------------------------------------------------------------------------
# Cross-category clusters: each link is two templates (one per category) that
# share a root event. shared_slots are held fixed across the whole cluster.
# ---------------------------------------------------------------------------
CROSS_CATEGORY_LINKS = {
    "vpn_outage": {
        "name": "VPN gateway outage",
        "scenarios": ["INF-11", "CE-11"],
        "shared_slots": ["vpn_gateway"],
    },
    "power_outage": {
        "name": "Site power outage",
        "scenarios": ["FAC-11", "LOG-12"],
        "shared_slots": ["site"],
    },
    "vendor_payment_hold": {
        "name": "Vendor payment run blocked",
        "scenarios": ["FIN-11", "VEN-11"],
        "shared_slots": ["vendor", "error_code"],
    },
    "sso_outage": {
        "name": "SSO identity provider outage",
        "scenarios": ["SEC-11", "CE-12"],
        "shared_slots": ["app"],
    },
    "wms_sync": {
        "name": "WMS inventory sync double-applied adjustments",
        "scenarios": ["INV-11", "LOG-11"],
        "shared_slots": ["site"],
    },
}

SCENARIOS = []


def _t(sid, cat, titles, desc, cause, fix, prio, anchor, extra=(), link=None):
    SCENARIOS.append({
        "id": sid,
        "category": cat,
        "team": CATEGORY_TEAM[cat],
        "titles": list(titles),
        "description": list(desc),
        "extra": list(extra),
        "cause": cause,
        "fix": fix,
        "priority": prio,
        "anchor": list(anchor),
        "link": link,
    })


# ------------------------------ Infrastructure ------------------------------
C = "Infrastructure"
_t("INF-01", C,
   ("Order API timeouts after database failover on {db_host}",
    "Intermittent {error_code} on order API following {db_host} failover"),
   ["The order API started returning {error_code} shortly after {db_host} failed over to its replica.",
    "Around {pct}% of order submissions from {site} failed during a window of roughly {hours} minutes."],
   "{db_host} failed over after a storage latency spike, and the connection pool on {server} kept pointing at the old primary.",
   "Restarted the pool on {server} and switched the connection string to the cluster endpoint. A failover drill was added to the monthly checklist.",
   "crit", ["db_host"],
   ["Customer portal order creation was also affected.",
    "Retries from the client made the load on the replica worse."])
_t("INF-02", C,
   ("Disk usage at 98% on {server}", "Log volume nearly full on {server}"),
   ["Monitoring alerted that the log volume on {server} reached 98% capacity.",
    "Application writes are failing intermittently with {error_code}."],
   "Log rotation was disabled by a configuration change, so debug logs grew unchecked.",
   "Compressed and archived the old logs, re-enabled logrotate and added an alert at 80% disk usage.",
   "med", ["server"],
   ["The same host had a similar alert two weeks ago."])
_t("INF-03", C,
   ("{app} processes restarting repeatedly on {server}",
    "Memory exhaustion on {server} restarting {app}"),
   ["The {app} service on {server} has restarted several times in the last {hours} hours.",
    "Memory usage climbs steadily until the process is killed by the out-of-memory handler."],
   "A memory leak in the caching layer of the latest {app} build exhausted the heap.",
   "Rolled back to the previous build and raised a defect for the leak. The fix is scheduled for the next release.",
   "high", ["server", "app"],
   ["Users at {site} saw slow responses before each restart."])
_t("INF-04", C,
   ("Expired TLS certificate on {service} endpoint",
    "Certificate expiry causing handshake failures on {service}"),
   ["Clients calling the {service} are failing TLS handshakes with {error_code}.",
    "The certificate on {server} expired earlier today."],
   "The auto-renewal job failed silently because the DNS challenge record had been removed.",
   "Issued a new certificate manually and restored the DNS record. Expiry alerts at 30 days were added.",
   "crit", ["service"],
   ["Two partner integrations had to be paused until the certificate was replaced."])
_t("INF-05", C,
   ("Nightly inventory sync job failing for {site}",
    "Inventory sync batch did not complete at {site}"),
   ["The nightly inventory sync for {site} has failed for the last {hours} runs.",
    "The job log shows it exited with {error_code} while waiting for the export file."],
   "The upstream export arrived late and the job's 30 minute wait window expired.",
   "Extended the wait window to 2 hours and added an automatic retry. Re-ran the sync manually for the missed days.",
   "med", ["site"])
_t("INF-06", C,
   ("Backups failing for {db_host}", "Nightly backup job errors on {db_host}"),
   ["Backups of {db_host} have failed for the past few nights with {error_code}.",
    "The last good backup is more than {hours} hours old."],
   "The backup service account password was rotated but the job credential was not updated.",
   "Updated the secret, ran a manual full backup and verified a restore into staging.",
   "high", ["db_host"])
_t("INF-07", C,
   ("DNS resolution failures for {service} from {site}",
    "Internal name lookups failing at {site} for {service}"),
   ["Hosts at {site} cannot resolve the internal name of the {service}.",
    "Other sites resolve it correctly, so the issue seems local to the resolver."],
   "A stale record after a migration was cached as NXDOMAIN on the secondary resolver at {site}.",
   "Flushed the resolver cache, corrected the record and lowered its TTL.",
   "med", ["site"])
_t("INF-08", C,
   ("Uneven traffic on load balancer, {server} saturated",
    "{server} overloaded while other nodes are idle"),
   ["Traffic is not spread evenly across the pool and {server} is at {pct}% above its normal load.",
    "Response times for the {service} are rising."],
   "The health check returned 200 on a degraded node, so the balancer kept routing to it.",
   "Updated the health check to test the downstream dependency and drained {server} for repair.",
   "med", ["server"])
_t("INF-09", C,
   ("High latency on WAN link to {site}", "Slow {app} for users at {site} due to WAN link"),
   ["Users at {site} report that {app} is very slow, with page loads of {hours} seconds or more.",
    "Latency to the data centre from {site} has tripled."],
   "The ISP fibre was cut and traffic fell back to the small backup circuit.",
   "The ISP repaired the fibre. The backup circuit bandwidth will be upsized.",
   "high", ["site"])
_t("INF-10", C,
   ("Alert storm from monitoring for {service}", "Too many duplicate alerts for {service}"),
   ["The on-call channel received more than {user_count} alerts for the {service} in an hour.",
    "Most alerts are duplicates and hide the real incident."],
   "A threshold change set the CPU alert to 40% so every normal batch spike paged the team.",
   "Reverted the threshold, grouped the alerts and added a 10 minute hold before paging.",
   "low", ["service"])
_t("INF-11", C,
   ("VPN gateway {vpn_gateway} unreachable, remote connections failing",
    "Remote users and partners cannot connect via {vpn_gateway}"),
   ["VPN connections through {vpn_gateway} have been failing since early this morning.",
    "Monitoring shows the IKE service on the gateway restarting in a loop."],
   "A firmware upgrade on {vpn_gateway} left the IKE service in a crash loop.",
   "Rolled the firmware back and moved traffic to the secondary gateway. The upgrade is deferred until the vendor ships a patch.",
   "crit", ["vpn_gateway"],
   ["Site-to-site tunnels to partners are also down."],
   link="vpn_outage")

# -------------------------------- Logistics ---------------------------------
C = "Logistics"
_t("LOG-01", C,
   ("Pallet shortage at {site} warehouse", "Not enough pallets for outbound loading at {site}"),
   ["The {site} warehouse has run out of standard pallets and outbound loading is slowing.",
    "Only about {qty} pallets are left against a daily need well above that."],
   "A pallet delivery from {vendor} was missed and the reorder point had been set too low.",
   "Transferred pallets from another site and raised the reorder point. {vendor} agreed to a weekly delivery slot.",
   "med", ["site"],
   ["Some orders were loaded on mixed pallets as a stopgap."])
_t("LOG-02", C,
   ("Shipment {shipment_no} delayed by {carrier}", "{carrier} missed pickup for {shipment_no}"),
   ["Shipment {shipment_no} was due out of {site} but {carrier} has not collected it.",
    "The customer has already been given a delivery date."],
   "A {carrier} truck broke down and no replacement vehicle was dispatched.",
   "Re-assigned the shipment to an alternate carrier and informed the customer of the new date.",
   "high", ["carrier"])
_t("LOG-03", C,
   ("Shipment {shipment_no} misrouted to the wrong hub",
    "Wrong hub assigned for {shipment_no} from {site}"),
   ["Shipment {shipment_no} was scanned at the wrong hub and is travelling away from its destination.",
    "{carrier} confirmed the routing label shows an incorrect hub code."],
   "A transposed hub code in the TMS mapping for {site} produced incorrect routing labels.",
   "Corrected the mapping, recalled the shipment and re-labelled the affected consignments.",
   "med", ["site"])
_t("LOG-04", C,
   ("{dock} congestion at {site}, trucks queuing", "Trucks waiting {hours} hours at {dock}, {site}"),
   ["Several trucks have queued at {dock} in {site} and loading is behind schedule.",
    "Drivers are reporting waits of up to {hours} hours."],
   "The appointment scheduler allowed overlapping slots for two carriers on the same dock.",
   "Blocked overlapping slots in the scheduler and staggered carrier arrival windows.",
   "med", ["site", "dock"])
_t("LOG-05", C,
   ("Damaged cartons on order {order_no}", "Customer received damaged goods on {order_no}"),
   ["Order {order_no} arrived with crushed cartons on at least {qty} units.",
    "Photos from the receiver show broken strapping on the pallet."],
   "Mixed pallets were stacked too high and strapped insufficiently at {site}.",
   "Re-shipped the damaged units and updated the packing standard for mixed pallets.",
   "med", ["site"])
_t("LOG-06", C,
   ("Handheld scanner {device} not syncing at {site}", "Scanner {device} offline in {site} warehouse"),
   ["Handheld scanner {device} will not sync with the WMS and picks cannot be confirmed.",
    "It shows {error_code} after login."],
   "The scanner firmware was outdated and its Wi-Fi profile had expired.",
   "Updated the firmware and re-provisioned the Wi-Fi profile. Other devices at {site} were checked.",
   "low", ["site"])
_t("LOG-07", C,
   ("Temperature excursion in cold storage at {site}", "Cold chain alarm at {site} warehouse"),
   ["The cold storage area at {site} reached {temp} degrees for about {hours} minutes.",
    "Chilled goods in that zone have been quarantined."],
   "A faulty door seal let warm air in and the compressor could not keep up.",
   "Replaced the door seal, serviced the compressor and released the stock after a quality check.",
   "high", ["site"])
_t("LOG-08", C,
   ("Order {order_no} stuck in Picking for {hours} hours", "Wave blocked, {order_no} not released from picking"),
   ["Order {order_no} has been in Picking for {hours} hours and will miss the cut-off.",
    "Other orders in the same wave are also waiting."],
   "A zero-quantity pick task blocked the whole wave in the WMS.",
   "Cancelled the bad task, released the wave and added a validation for zero-quantity picks.",
   "high", ["site"])
_t("LOG-09", C,
   ("Returns backlog of {qty} items at {site}", "Returns not processed at {site}"),
   ["Returns processing at {site} has a backlog of about {qty} items.",
    "Customers are waiting for refunds because items are not yet inspected."],
   "A shift pattern change left the returns inspection team short-staffed.",
   "Added a temporary second shift and cleared the backlog over three days.",
   "low", ["site"])
_t("LOG-10", C,
   ("E-way bill generation failing for {shipment_no}", "Cannot generate e-way bill for {shipment_no}"),
   ["The e-way bill for {shipment_no} fails with {error_code} so the truck cannot leave {site}.",
    "Several other consignments are queued behind it."],
   "The vehicle number was entered in an invalid format that the tax portal rejects.",
   "Corrected the vehicle number format and added input validation on the dispatch screen.",
   "med", ["site"])
_t("LOG-11", C,
   ("Pickers short on SKUs shown as available at {site}", "Pick shortages at {site} after inventory sync"),
   ["Pickers at {site} are finding empty bins for items the WMS shows as available.",
    "Around {qty} order lines have been short-picked in the last {hours} hours."],
   "A WMS sync job at {site} double-applied inventory adjustments after a retry.",
   "Reversed the duplicated adjustments, fixed the retry logic and re-counted the affected bins.",
   "high", ["site"], link="wms_sync")
_t("LOG-12", C,
   ("Outbound dispatch halted at {site} after power outage", "No dispatch from {site}, power failure"),
   ["Outbound dispatch at {site} stopped when power failed this morning.",
    "Conveyors and dock doors are not operating and {shipment_no} is among the delayed shipments."],
   "A grid failure at {site} coincided with a faulty transfer switch, so the generator never took the load.",
   "Restarted operations once power was restored and re-planned the missed dispatches for the next wave.",
   "crit", ["site"], link="power_outage")

# -------------------------------- Inventory ---------------------------------
C = "Inventory"
_t("INV-01", C,
   ("Stock count mismatch for {sku} at {site}", "System stock of {sku} does not match physical at {site}"),
   ["The physical count of {sku} at {site} is {qty} units off from the system quantity.",
    "The variance was found during a routine bin check."],
   "A goods receipt was physically completed but never posted in the WMS.",
   "Posted the missing receipt and added a daily report for unposted receipts.",
   "med", ["site"])
_t("INV-02", C,
   ("Stockout of {sku} causing backorders", "{sku} out of stock, orders backordered"),
   ["{sku} is out of stock at {site} and {qty} units are now on backorder.",
    "Customers are being told to expect delays."],
   "The demand forecast did not include a promotion that doubled weekly sales.",
   "Expedited a replenishment order and updated the forecast with the promotion calendar.",
   "high", ["sku"])
_t("INV-03", C,
   ("Near-expiry batch of {sku} found at {site}", "Expiring stock of {sku} still in pick faces"),
   ["A batch of {sku} at {site} expires within {hours} days and is still in the primary pick location.",
    "About {qty} units are affected."],
   "First-expiry-first-out picking was not enforced for this item class.",
   "Moved the batch to priority picking, enabled FEFO rules for the item class and notified sales.",
   "med", ["site"])
_t("INV-04", C,
   ("Duplicate SKU records for {sku}", "Item master has two entries for {sku}"),
   ["Two item master records exist for {sku} and stock is split between them.",
    "Reports show different on-hand quantities depending on which record is used."],
   "A bulk item upload was run twice and created duplicate records.",
   "Merged the records, moved the stock to the surviving one and added a duplicate check to uploads.",
   "low", ["sku"])
_t("INV-05", C,
   ("Cycle count variance of {pct}% in a zone at {site}", "High variance in cycle count, {site}"),
   ["The latest cycle count at {site} shows a {pct}% variance in one storage zone.",
    "The same zone had a smaller variance in the previous count."],
   "Items were repeatedly slotted into neighbouring bins that look alike.",
   "Relabelled the bins, retrained the putaway team and re-counted the zone.",
   "med", ["site"])
_t("INV-06", C,
   ("Negative on-hand quantity for {sku}", "{sku} shows negative stock at {site}"),
   ["{sku} shows a negative on-hand quantity at {site}.",
    "This blocks new allocations for the item."],
   "Shipments were confirmed before the matching goods receipt was posted.",
   "Posted the receipt, corrected the balance and enforced the receipt-before-ship rule.",
   "med", ["sku"])
_t("INV-07", C,
   ("{qty} units of {sku} missing from bin at {site}", "Cannot find stock of {sku} that the system shows"),
   ["{qty} units of {sku} are recorded in a bin at {site} but cannot be found.",
    "A search of adjacent aisles found nothing."],
   "Putaway went to a wrong bin because the label printer produced a misaligned barcode.",
   "Located the stock in the neighbouring aisle and replaced the faulty printer head.",
   "low", ["site"])
_t("INV-08", C,
   ("Reorder point for {sku} too low", "Replenishment for {sku} triggered too late"),
   ["The reorder point for {sku} is not covering the lead time and stock runs out before the PO arrives.",
    "This is the second stock shortage for the item this quarter."],
   "The supplier lead time increased but the planning parameters were never updated.",
   "Raised the reorder point and safety stock based on the new lead time.",
   "med", ["sku"])
_t("INV-09", C,
   ("Transfer to {site} not received, {qty} units in transit", "Inter-site transfer stuck in transit to {site}"),
   ["A transfer of {qty} units to {site} still shows in transit although the truck arrived days ago.",
    "The receiving team cannot book the stock."],
   "The advance shipping notice was never posted so the receipt could not be matched.",
   "Posted the missing notice manually and received the transfer.",
   "med", ["site"])
_t("INV-10", C,
   ("Wrong barcode labels printed for {sku}", "Labels for {sku} scan as a different item"),
   ["Labels printed for {sku} scan as another item at the {site} packing station.",
    "About {qty} labelled cartons are affected."],
   "The label template was mapped to an outdated barcode field.",
   "Corrected the template mapping, reprinted the labels and quarantined the wrong ones.",
   "low", ["sku"])
_t("INV-11", C,
   ("Stock mismatch for {sku} after WMS sync at {site}", "Inventory doubled for {sku} following sync at {site}"),
   ["On-hand for {sku} at {site} jumped by {qty} units after last night's WMS sync, with no matching receipt.",
    "Several other SKUs at the site show similar increases."],
   "A WMS sync job at {site} double-applied inventory adjustments after a retry.",
   "Reversed the duplicate adjustments and re-counted the affected SKUs.",
   "high", ["site"], link="wms_sync")

# ---------------------------- Vendor Management -----------------------------
C = "Vendor Management"
_t("VEN-01", C,
   ("Invoice {invoice_no} from {vendor} does not match {po_no}", "Price mismatch between {invoice_no} and {po_no}"),
   ["Invoice {invoice_no} from {vendor} is higher than the value of {po_no}.",
    "The invoice is on hold until the difference is explained."],
   "{vendor} applied a new price list that had not been loaded into the PO.",
   "Agreed the revised prices in writing, updated the PO and released the invoice.",
   "med", ["vendor"])
_t("VEN-02", C,
   ("{vendor} delivery late against {po_no}", "Late delivery from {vendor}, {po_no}"),
   ["Material for {po_no} from {vendor} has not arrived and is now {hours} days late.",
    "Production planning at {site} is waiting on this delivery."],
   "{vendor} had a capacity shortage after a machine breakdown at their plant.",
   "Split the balance across two shipments and expedited the first. A recovery plan was agreed with {vendor}.",
   "high", ["vendor"],
   ["This is not the first late delivery from this vendor."])
_t("VEN-03", C,
   ("Quality rejection of {qty} units from {vendor}", "Incoming inspection failed for {vendor} lot"),
   ["Incoming inspection rejected {qty} units from {vendor} against {po_no}.",
    "Defects include misprinted labels and uneven sealing."],
   "{vendor} changed a material grade without notifying us.",
   "Returned the lot for replacement and added the material grade to the approved specification.",
   "med", ["vendor"])
_t("VEN-04", C,
   ("Contract with {vendor} expires next month", "No renewal started for {vendor} contract"),
   ["The supply contract with {vendor} expires next month and no renewal has been started.",
    "Current pricing and delivery terms would lapse."],
   "The renewal reminder was assigned to a buyer who left the team.",
   "Reassigned the contract owner, extended the term by 60 days and started the renewal negotiation.",
   "low", ["vendor"])
_t("VEN-05", C,
   ("Onboarding of {vendor} blocked", "New vendor {vendor} cannot be activated"),
   ["The onboarding of {vendor} is stuck and no PO can be raised.",
    "The compliance check shows missing documents."],
   "The GST certificate on file was unreadable and the check could not be completed.",
   "Collected a fresh certificate, completed the compliance check and activated the vendor.",
   "low", ["vendor"])
_t("VEN-06", C,
   ("{vendor} short-shipped {po_no}", "Quantity shortfall on {po_no} from {vendor}"),
   ["{po_no} from {vendor} was delivered {qty} units short of the ordered quantity.",
    "The delivery note shows the full quantity."],
   "The vendor dispatch team loaded from an earlier version of the order.",
   "Raised a shortage claim, got the balance shipped and asked {vendor} to confirm the latest PO revision before dispatch.",
   "med", ["vendor"])
_t("VEN-07", C,
   ("Unannounced price increase from {vendor}", "{vendor} invoicing above agreed rates"),
   ["{vendor} has started invoicing about {pct}% above the agreed rates.",
    "No notice or amendment was received."],
   "{vendor} applied an annual escalation clause that does not exist in the current contract.",
   "Rejected the increase, re-issued the contract terms and requested corrected invoices.",
   "med", ["vendor"])
_t("VEN-08", C,
   ("{vendor} cannot access the vendor portal", "Vendor portal login failing for {vendor}"),
   ["Users from {vendor} cannot log in to the vendor portal and cannot submit shipment notices.",
    "They see {error_code} after entering the code."],
   "The vendor accounts were locked after repeated failed MFA attempts.",
   "Unlocked the accounts and re-enrolled the MFA devices.",
   "low", ["vendor"])
_t("VEN-09", C,
   ("No ASN received from {vendor} for {po_no}", "Missing advance shipping notice, {po_no}"),
   ["{vendor} shipped against {po_no} but no advance shipping notice was received.",
    "The truck arrived at {site} and the receiving team could not match it."],
   "{vendor} sent the notice to an old mailbox that is no longer monitored.",
   "Updated the vendor contact, posted the notice manually and received the goods.",
   "low", ["vendor"])
_t("VEN-10", C,
   ("{vendor} on-time delivery at {pct}% below target", "Repeated SLA breach by {vendor}"),
   ["On-time delivery from {vendor} has fallen {pct}% below target for the quarter.",
    "Multiple POs including {po_no} were affected."],
   "{vendor} moved production to a new plant with an unstable transport schedule.",
   "Held a service review, agreed a corrective plan and moved part of the volume to a second vendor.",
   "med", ["vendor"])
_t("VEN-11", C,
   ("{vendor} putting supplies on hold over unpaid invoices", "Supply hold threatened by {vendor}"),
   ["{vendor} has warned it will hold shipments because invoices are overdue.",
    "The affected invoices include {invoice_no}."],
   "The payment run for {vendor} was rejected by the bank with error {error_code} and the invoices were never re-paid.",
   "Corrected the bank details, paid the overdue invoices and confirmed shipping with {vendor}.",
   "high", ["vendor", "error_code"], link="vendor_payment_hold")

# ---------------------------- Customer Escalation ---------------------------
C = "Customer Escalation"
_t("CE-01", C,
   ("{customer} escalation: order {order_no} not delivered", "{customer} chasing undelivered order {order_no}"),
   ["{customer} has escalated that order {order_no} has not been delivered by the promised date.",
    "Their operations head asked for a status update today."],
   "The order was held in picking because of a stock allocation error at {site}.",
   "Expedited the order by courier and shared a revised tracking link with {customer}.",
   "high", ["customer"])
_t("CE-02", C,
   ("{customer} received wrong item on {order_no}", "Wrong SKU delivered to {customer}"),
   ["{customer} received {sku} instead of the item ordered on {order_no}.",
    "They need the correct goods urgently."],
   "A look-alike SKU was picked because the bin labels at {site} were swapped.",
   "Shipped the correct item by priority, collected the wrong one and fixed the bin labels.",
   "med", ["customer"])
_t("CE-03", C,
   ("{customer} requests SLA credit for missed delivery windows", "SLA credit claim from {customer}"),
   ["{customer} is asking for a credit because {pct}% of deliveries missed the contracted window last month.",
    "They have copied their procurement director."],
   "Delivery delays on the {carrier} lane caused the misses.",
   "Verified the delivery data, approved a partial credit and agreed a lane recovery plan.",
   "high", ["customer"])
_t("CE-04", C,
   ("{customer} third complaint about {carrier}", "Repeated delivery complaints from {customer} on {carrier}"),
   ["{customer} has complained for the third time about late and damaged deliveries by {carrier}.",
    "The latest affected shipment is {shipment_no}."],
   "{carrier} used an under-resourced route for the customer area with frequent driver changes.",
   "Switched the customer to a different carrier and assigned a dedicated account manager.",
   "high", ["customer", "carrier"])
_t("CE-05", C,
   ("Tracking for {shipment_no} not updating", "{customer} cannot see updates on {shipment_no}"),
   ["Tracking for {shipment_no} has shown the same status for {hours} hours.",
    "{customer} is asking whether the goods are lost."],
   "The carrier tracking feed to the TMS stopped updating after an API token expired.",
   "Renewed the token, backfilled the missed scans and informed {customer}.",
   "med", ["customer"])
_t("CE-06", C,
   ("{customer} damage claim for order {order_no}", "Damaged goods claim, {customer}"),
   ["{customer} has filed a claim for damaged goods on order {order_no}.",
    "They are asking for replacement of about {qty} units."],
   "Insufficient edge protection on the pallet led to crushed cartons in transit.",
   "Approved the claim, shipped replacements and updated the packing guidelines.",
   "med", ["customer"])
_t("CE-07", C,
   ("Executive escalation from {customer} on onboarding delays", "{customer} unhappy with go-live delays"),
   ["{customer} has escalated to senior management about repeated slips in their onboarding.",
    "They are threatening to move volume to another provider."],
   "Integration testing was delayed because two requirements were unclear and nobody owned the follow-up.",
   "Appointed a single project owner, agreed a dated plan and held weekly calls until go-live.",
   "crit", ["customer"])
_t("CE-08", C,
   ("{customer} users cannot log in to the customer portal", "Portal logins failing for {customer}"),
   ["Several users at {customer} cannot sign in to the customer portal.",
    "They receive a reset email but the link is expired on arrival."],
   "A password policy change forced resets and the reset emails were delayed by the mail relay.",
   "Cleared the mail queue, extended the reset link validity and manually reset the stuck users.",
   "med", ["customer"])
_t("CE-09", C,
   ("{customer} missing proof of delivery for {order_no}", "Proof of delivery not available, {order_no}"),
   ["{customer} needs proof of delivery for {order_no} and cannot find it in the portal.",
    "Their finance team is holding payment until they receive it."],
   "The signed delivery slip was never scanned by the carrier depot.",
   "Retrieved the paper copy from the depot, uploaded it and reminded {carrier} to scan on delivery.",
   "low", ["customer"])
_t("CE-10", C,
   ("{customer} order cancelled after stock promise on {sku}", "Availability for {sku} promised to {customer} but not honoured"),
   ["{customer} was told {sku} was available but the order was cancelled for lack of stock.",
    "They had already planned a promotion around it."],
   "The availability shown to the account team did not account for stock already allocated to other orders.",
   "Sourced the stock from another site and recovered the order, with a goodwill discount.",
   "high", ["customer"])
_t("CE-11", C,
   ("{customer} cannot reach the integration endpoint", "{customer} integration tunnel down"),
   ["{customer} reports that their systems cannot connect to our order integration endpoint.",
    "Their site-to-site VPN through {vpn_gateway} is down and orders are queuing on their side."],
   "The VPN gateway {vpn_gateway} outage cut off partner connectivity to the integration endpoint.",
   "Connectivity was restored when traffic moved to the secondary gateway, and queued orders were replayed.",
   "crit", ["vpn_gateway"], link="vpn_outage")
_t("CE-12", C,
   ("{customer} users locked out of {app} login", "{customer} cannot sign in to {app}"),
   ["Users at {customer} cannot sign in to {app} through single sign-on.",
    "They get an authentication error right after the login page."],
   "The SSO identity provider certificate expired, so all federated logins failed.",
   "Replaced the certificate on the identity provider and confirmed logins with {customer}.",
   "crit", ["app"], link="sso_outage")

# ----------------------------- Access & Security ----------------------------
C = "Access & Security"
_t("SEC-01", C,
   ("Multiple account lockouts at {site}", "Users at {site} locked out of their accounts"),
   ["About {user_count} accounts at {site} were locked out in the last hour.",
    "Users say they did not enter wrong passwords."],
   "A cached credential on a shared kiosk kept retrying an old password and triggered the lockout policy.",
   "Cleared the cached credentials, unlocked the accounts and adjusted the lockout threshold for kiosks.",
   "med", ["site"])
_t("SEC-02", C,
   ("Badge access failing at {zone}", "Employees cannot badge in at {zone}"),
   ["Employee badges are not opening the door at {zone}.",
    "The reader beeps but the lock does not release."],
   "The access controller lost time synchronisation after a firmware update, rejecting valid schedules.",
   "Resynced the controller clock, re-pushed the schedules and tested with five badges.",
   "high", ["zone"],
   ["Security guards are letting people in manually in the meantime."])
_t("SEC-03", C,
   ("Phishing email reported by {user_count} users", "Phishing campaign targeting staff"),
   ["{user_count} users reported an email asking them to confirm their credentials.",
    "A few users may have clicked the link."],
   "A look-alike sender domain passed the mail filter because it was not yet on the block list.",
   "Blocked the domain, purged the emails from mailboxes and reset passwords for users who clicked.",
   "high", ["service"])
_t("SEC-04", C,
   ("Admin access request for {app} overdue", "Privileged access for {app} not granted"),
   ["An administrator access request for {app} has been pending for {hours} days.",
    "The requester cannot complete a scheduled change."],
   "The approval workflow was routed to a manager who had moved teams.",
   "Corrected the approver, granted time-limited access and updated the routing table.",
   "low", ["app"])
_t("SEC-05", C,
   ("MFA push not delivered to users at {site}", "MFA prompts not arriving at {site}"),
   ["Users at {site} are not receiving MFA push notifications and cannot log in.",
    "SMS fallback works but is slow."],
   "The push service was blocked by a firewall rule applied at {site}.",
   "Allowed the push service addresses on the firewall and tested with affected users.",
   "high", ["site"])
_t("SEC-06", C,
   ("Leaver still has active access to {app}", "Offboarded employee retained {app} access"),
   ["An employee who left last week still has an active account in {app}.",
    "This was found during the weekly access review."],
   "The HR leaver feed to the identity system was delayed by several days.",
   "Disabled the account, reviewed the activity log and added a daily reconciliation against HR.",
   "high", ["app"])
_t("SEC-07", C,
   ("Suspicious logins to {app} from unusual locations", "Unusual sign-in pattern on {app}"),
   ["Several logins to {app} came from locations outside the company's usual countries.",
    "The accounts involved belong to the {site} team."],
   "Credentials from an external breach were reused on corporate accounts.",
   "Forced password resets, enabled conditional access by country and notified affected users.",
   "crit", ["app"])
_t("SEC-08", C,
   ("Endpoint protection outdated on {user_count} laptops", "Antivirus agents not updating"),
   ["{user_count} laptops report an endpoint protection version that is several releases behind.",
    "They failed the weekly compliance scan."],
   "A proxy rule blocked the update channel for the agent.",
   "Fixed the proxy rule and forced the update on the affected devices.",
   "med", ["site"])
_t("SEC-09", C,
   ("Firewall change blocks {service} traffic from {site}", "{service} unreachable from {site} after firewall change"),
   ["After yesterday's firewall change users at {site} cannot reach the {service}.",
    "The error is {error_code}."],
   "A rule was added with a deny that sat above the existing allow for the service subnet.",
   "Reordered the rules, tested access from {site} and added a peer review step to firewall changes.",
   "high", ["site"])
_t("SEC-10", C,
   ("Shared account found on {server}", "Audit found shared login on {server}"),
   ["The quarterly audit found a shared administrator account in use on {server}.",
    "Several people know the password."],
   "The account was created for a legacy script and never replaced with a named service account.",
   "Created a named service account, rotated credentials and disabled the shared login.",
   "low", ["server"])
_t("SEC-11", C,
   ("SSO login failing for staff across {app}", "Single sign-on broken for {app}"),
   ["Staff cannot sign in to {app} through single sign-on and see an authentication error.",
    "Local logins still work."],
   "The SSO identity provider certificate expired, so all federated logins failed.",
   "Replaced the certificate on the identity provider and verified sign-in to {app}.",
   "crit", ["app"], link="sso_outage")

# -------------------------------- Facilities --------------------------------
C = "Facilities"
_t("FAC-01", C,
   ("HVAC failure in the server room at {site}", "Cooling down in server room, {site}"),
   ["The server room at {site} is above normal temperature and the air conditioning has stopped.",
    "Temperature reached {temp} degrees above the usual setting."],
   "A tripped compressor contactor shut down the primary cooling unit.",
   "Replaced the contactor, restarted the unit and added a temperature alert to the building system.",
   "crit", ["site"])
_t("FAC-02", C,
   ("Water leak in the ceiling at {site}", "Roof leak near {zone}"),
   ["Water is dripping from the ceiling near {zone} at {site} after the rain.",
    "Wet floor markers have been placed."],
   "A blocked rainwater outlet on the roof caused water to pool and seep through a joint.",
   "Cleared the outlet, sealed the joint and inspected the other outlets.",
   "med", ["site"])
_t("FAC-03", C,
   ("Lighting out in warehouse aisles at {site}", "Dark aisles in {site} warehouse"),
   ["Several aisles in the {site} warehouse have no lighting.",
    "Staff are working with torches, which is a safety risk."],
   "A tripped circuit breaker on the lighting distribution board.",
   "Reset the breaker, replaced two faulty fixtures and checked the remaining circuits.",
   "med", ["site"])
_t("FAC-04", C,
   ("False fire alarm triggers at {site}", "Fire alarm panel repeatedly triggering at {site}"),
   ["The fire alarm at {site} has triggered {hours} times this week without any fire.",
    "Staff are being evacuated each time and operations are disrupted."],
   "Dust accumulation made a smoke detector near the loading area oversensitive.",
   "Cleaned and recalibrated the detector, then tested the panel zone.",
   "high", ["site"])
_t("FAC-05", C,
   ("Lift out of service at HQ", "HQ lift not working"),
   ["The main lift at HQ is out of service and visitors are being directed to the stairs.",
    "Staff with mobility needs are affected."],
   "A door sensor fault tripped the safety circuit.",
   "The lift contractor replaced the sensor and completed a safety check.",
   "low", ["zone"])
_t("FAC-06", C,
   ("Pest activity reported near storage area at {site}", "Rodent sightings in {site} warehouse"),
   ["Rodent droppings were found near the packaging storage at {site}.",
    "Some cartons may be contaminated."],
   "Gaps under a dock door gave access to pests.",
   "Pest control treated the area, door gaps were sealed and the packaging was inspected.",
   "med", ["site"])
_t("FAC-07", C,
   ("Dock leveller failure at {dock}, {site}", "{dock} leveller stuck at {site}"),
   ["The dock leveller at {dock} in {site} is stuck and trucks cannot be unloaded.",
    "Staff are using another dock which increases the waiting time."],
   "A hydraulic hose burst and drained the fluid.",
   "Replaced the hose, refilled the fluid and tested the leveller under load.",
   "high", ["site", "dock"])
_t("FAC-08", C,
   ("CCTV cameras offline at {site}", "No CCTV feed at {site}"),
   ["Several CCTV cameras at {site} show no feed in the control room.",
    "The gaps include the dispatch area."],
   "A failed network switch cut power to the cameras.",
   "Replaced the switch and restored the feeds.",
   "med", ["site"])
_t("FAC-09", C,
   ("Cleaning not done at {zone}", "Housekeeping contractor missed {zone}"),
   ["The scheduled cleaning of {zone} was not done for two days.",
    "Staff have complained about the state of the washrooms."],
   "The contractor had a staff shortage and prioritised another site.",
   "The contractor sent a cleaning crew the same day and committed to a backup roster.",
   "low", ["zone"])
_t("FAC-10", C,
   ("Boom barrier at gate not working at {site}", "Gate barrier stuck at {site}"),
   ["The boom barrier at the main gate of {site} is stuck open.",
    "Security is checking vehicles manually."],
   "A worn limit switch left the barrier arm unable to close.",
   "Replaced the limit switch and tested the barrier.",
   "low", ["site"])
_t("FAC-11", C,
   ("Power failure at {site} facility, generator did not start", "{site} site without power"),
   ["The {site} facility has lost power and the backup generator did not start.",
    "Lighting, dock doors and servers are affected."],
   "A grid failure at {site} coincided with a faulty transfer switch, so the generator never took the load.",
   "The utility restored supply and the transfer switch was replaced. A monthly generator load test was scheduled.",
   "crit", ["site"], link="power_outage")

# ----------------------------- Billing & Finance ----------------------------
C = "Billing & Finance"
_t("FIN-01", C,
   ("Duplicate payment to {vendor} for {invoice_no}", "{vendor} paid twice on {invoice_no}"),
   ["{vendor} was paid twice for {invoice_no}, an overpayment of INR {amount}.",
    "The vendor confirmed receipt of both payments."],
   "The invoice was entered twice with slightly different reference numbers, bypassing the duplicate check.",
   "Recovered the excess through a credit against the next invoice and tightened the duplicate check.",
   "med", ["vendor"])
_t("FIN-02", C,
   ("Tax mismatch on invoice {invoice_no}", "GST amount incorrect on {invoice_no}"),
   ["The GST on {invoice_no} does not match the expected rate and the invoice is blocked.",
    "The amount involved is INR {amount}."],
   "The wrong tax code was applied to the line item in the billing system.",
   "Corrected the tax code, re-issued the invoice and reran the tax reconciliation.",
   "med", ["customer"])
_t("FIN-03", C,
   ("Bank payment file rejected with {error_code}", "Payment run failed, bank returned {error_code}"),
   ["The vendor payment file was rejected by the bank with {error_code}.",
    "{qty} payments are pending."],
   "A new beneficiary account number had the wrong format and invalidated the whole batch.",
   "Fixed the beneficiary entry and resubmitted the payment file.",
   "high", ["error_code"])
_t("FIN-04", C,
   ("{customer} credit note not applied to {invoice_no}", "Credit note missing on {invoice_no}"),
   ["{customer} says the credit note for {invoice_no} was not applied.",
    "They have paid the full amount and expect a refund of INR {amount}."],
   "The credit note was approved but never posted against the open invoice.",
   "Posted the credit note and refunded the difference.",
   "med", ["customer"])
_t("FIN-05", C,
   ("Invoice {invoice_no} raised at wrong exchange rate", "Wrong FX rate used for {invoice_no}"),
   ["{invoice_no} was raised using last month's exchange rate and the amount is off by INR {amount}.",
    "The customer has noticed the difference."],
   "The exchange rate table was not refreshed after month-end.",
   "Refreshed the rates, re-issued the invoice and added a check on the rate date.",
   "low", ["customer"])
_t("FIN-06", C,
   ("Month-end reconciliation break of INR {amount}", "Unreconciled difference at month-end"),
   ["The month-end reconciliation shows an unexplained difference of INR {amount}.",
    "It involves a batch of freight invoices."],
   "A batch of invoices was posted to the wrong cost centre during an import.",
   "Moved the postings to the right cost centre and cleared the difference.",
   "med", ["site"])
_t("FIN-07", C,
   ("Refund to {customer} pending for over a week", "{customer} refund of INR {amount} not paid"),
   ["{customer} has been waiting more than a week for a refund of INR {amount}.",
    "They have contacted the account team twice."],
   "The refund was approved but stuck in the payment queue because of missing bank details.",
   "Obtained the bank details, released the refund and informed {customer}.",
   "med", ["customer"])
_t("FIN-08", C,
   ("{po_no} stuck in approval", "Purchase order {po_no} waiting for approval for {hours} days"),
   ["{po_no} has been waiting in the approval workflow for {hours} days.",
    "The vendor will not ship without a PO."],
   "The approver was on leave and the delegation was not set.",
   "Approved via the delegate and fixed the delegation rule.",
   "low", ["vendor"])
_t("FIN-09", C,
   ("{customer} disputes charges on {invoice_no}", "Billing dispute from {customer} on {invoice_no}"),
   ["{customer} disputes INR {amount} of charges on {invoice_no}.",
    "They say the storage fees were billed for a period when goods had already left."],
   "The storage billing run used the receipt date instead of the actual dispatch date.",
   "Recalculated the fees, issued a credit note and corrected the billing rule.",
   "med", ["customer"])
_t("FIN-10", C,
   ("Freight invoice from {carrier} exceeds rate card", "Overcharge on {carrier} invoice {invoice_no}"),
   ["The freight invoice {invoice_no} from {carrier} is about {pct}% above the agreed rate card.",
    "The total difference is INR {amount}."],
   "{carrier} applied a fuel surcharge that is not part of the contract.",
   "Disputed the surcharge, received a corrected invoice and paid the correct amount.",
   "low", ["carrier"])
_t("FIN-11", C,
   ("Payment run held for {vendor}, invoices overdue", "Vendor payments to {vendor} not released"),
   ["Payments to {vendor} did not go out in the last run and invoices like {invoice_no} are overdue.",
    "The bank file shows {error_code}."],
   "The payment run for {vendor} was rejected by the bank with error {error_code} and the invoices were never re-paid.",
   "Corrected the bank details and re-ran the payment for {vendor}.",
   "high", ["vendor", "error_code"], link="vendor_payment_hold")

# ---------------------------------------------------------------------------
# Deliberately vague, low-quality tickets (category given for balance).
# ---------------------------------------------------------------------------
VAGUE_TICKETS = [
    ("Not working", "System not working again. Please check.", "Infrastructure"),
    ("Slow", "Everything is slow today.", "Infrastructure"),
    ("Server issue", "Getting some error on the server, same as before.", "Infrastructure"),
    ("Delay", "Shipment delayed, need update asap.", "Logistics"),
    ("Truck problem", "Truck issue at the warehouse. Pls look.", "Logistics"),
    ("Where is my order", "Customer asking about order. Need help.", "Logistics"),
    ("Stock wrong", "Stock is not matching. Please fix.", "Inventory"),
    ("Items missing", "Some items not found.", "Inventory"),
    ("Inventory issue", "Same issue as last time with inventory.", "Inventory"),
    ("Vendor issue", "Vendor not responding. Please follow up.", "Vendor Management"),
    ("Invoice problem", "Invoice has some mistake.", "Vendor Management"),
    ("Supplier delay", "Supplier late again.", "Vendor Management"),
    ("Customer upset", "Customer is not happy, call them.", "Customer Escalation"),
    ("Escalation", "Please check urgently.", "Customer Escalation"),
    ("Complaint", "Client complaining about service.", "Customer Escalation"),
    ("Cannot login", "Unable to login, please help.", "Access & Security"),
    ("Access needed", "Need access to the system.", "Access & Security"),
    ("Security", "Something looks suspicious, please check.", "Access & Security"),
    ("AC not working", "AC problem in office.", "Facilities"),
    ("Light issue", "Light not working.", "Facilities"),
    ("Building issue", "Please send someone to check.", "Facilities"),
    ("Payment issue", "Payment not received.", "Billing & Finance"),
    ("Wrong amount", "Amount is wrong in the bill.", "Billing & Finance"),
    ("Finance query", "Need to check something with finance.", "Billing & Finance"),
]

VAGUE_RESOLUTIONS = [
    "Could not reproduce the issue. Closed after no further response from the requester.",
    "Restarted the service and the user confirmed it was working.",
    "Duplicate of an earlier request and closed.",
    "Requester provided details by phone and the issue was fixed.",
]

# ---------------------------------------------------------------------------
# Generic comment text for tickets (slot-free).
# ---------------------------------------------------------------------------
COMMENT_POOLS = {
    "triage": [
        "Picked this up and starting the investigation.",
        "Looking into it now, will share an update shortly.",
        "Assigning to the right team and checking the logs.",
        "Can you share more details on when this started?",
    ],
    "progress": [
        "Found a likely cause and working on a fix.",
        "Waiting on a response from the external party.",
        "Applied a temporary workaround while we work on the permanent fix.",
        "Issue is still occurring. Escalating to the senior engineer.",
        "Checked with the site team and they confirm the same behaviour.",
    ],
    "requester": [
        "Still seeing this issue on our side.",
        "Any update? This is blocking our work.",
        "Thanks, it looks better now but please keep this open until tomorrow.",
        "Confirming this is affecting more than one person.",
    ],
    "resolved": [
        "Fix applied and verified. Closing this out.",
        "Confirmed with the requester that the issue is resolved.",
        "Permanent fix deployed. Monitoring for recurrence.",
        "Root cause documented in the resolution notes.",
    ],
}


# ---------------------------------------------------------------------------
# Extra detail sentences (reporter's side, impact, affected unit counts) for
# templates whose core text has little variety. Merged into each template's
# "extra" pool below; the generator picks 1-2 per ticket. They only use slots
# that are not held fixed in a cluster, so cluster members differ in wording.
# ---------------------------------------------------------------------------
EXTRA_DETAILS = {
    "INF-01": [
        "Our team at {site} counted {qty} orders that had to be re-entered manually.",
        "Support has received {user_count} tickets from users about failed checkouts.",
        "Finance estimates about INR {amount} in delayed order value.",
    ],
    "INF-02": [
        "Free space dropped by about {pct}% in the last {hours} hours.",
        "{user_count} users of the service were warned about possible errors.",
        "The largest log file alone holds about {qty} MB of debug output.",
    ],
    "INF-03": [
        "The on-call engineer counted {qty} restarts since the first alert.",
        "About {user_count} users reported slow screens or dropped sessions.",
        "Throughput has fallen by roughly {pct}% compared with last week.",
    ],
    "INF-04": [
        "{user_count} client applications are showing a certificate warning.",
        "At least {qty} API calls were rejected before the first alert fired.",
        "Partner support has asked for an update within {hours} hours.",
    ],
    "INF-05": [
        "About {qty} inventory records were not refreshed for the site.",
        "The morning planners are working from stock numbers that are {hours} hours old.",
        "{user_count} users rely on the report that this job feeds.",
        "Nobody was alerted when the job failed, and a planner found it.",
    ],
    "INF-06": [
        "The database holds data for roughly {qty} active customer accounts.",
        "The recovery point objective is now exceeded by about {hours} hours.",
        "The backup log shows no completed run since the failures began.",
        "Audit asked whether a recent restore test has been done.",
    ],
    "INF-07": [
        "{user_count} users at the site are affected.",
        "Using the IP address directly works but is not sustainable.",
        "The resolver log shows {qty} failed lookups in the last hour.",
        "Staff at the site have been unable to open the related screens for about {hours} hours.",
    ],
    "INF-08": [
        "About {qty} requests per minute are being queued on the overloaded node.",
        "{user_count} users have reported slow pages in the last hour.",
        "Other nodes in the pool are running at less than {pct}% of their capacity.",
        "The issue gets worse during the afternoon peak.",
    ],
    "INF-09": [
        "{user_count} staff at the site are affected, including the dispatch desk.",
        "Large file transfers to head office have been timing out for about {hours} hours.",
        "Video calls from the site keep dropping.",
        "Packet loss on the link is around {pct}%.",
    ],
    "INF-11": [
        "{user_count} remote employees have been unable to reach internal systems.",
        "Partners with scheduled file transfers are asking about {qty} pending files.",
        "Support has received calls from users every few minutes since the outage began.",
    ],
    "LOG-04": [
        "Driver waiting time is costing about INR {amount} in detention charges.",
        "{qty} units are sitting on trucks that have not been unloaded yet.",
        "Two inbound appointments were pushed back because of the queue.",
        "Trucks are blocking the yard road so other docks are slowing down.",
    ],
    "LOG-07": [
        "About {qty} cartons of chilled goods were in the affected area.",
        "The goods are worth about INR {amount}.",
        "The temperature logger shows the alarm was not acknowledged for a while.",
        "Customers expecting these goods have been told to expect a delay.",
    ],
    "INV-05": [
        "The variance covers about {qty} units across several SKUs.",
        "The adjustment would be worth around INR {amount} if confirmed.",
        "The count team recounted {pct}% of the bins to confirm the numbers.",
        "The inventory controller wants a root cause before posting the adjustment.",
    ],
    "INV-04": [
        "About {qty} units of stock are split across the two records.",
        "{user_count} users have seen different on-hand quantities for the item in their reports.",
        "The planning team at {site} flagged the mismatch while reviewing a stock valuation of INR {amount}.",
        "Order {order_no} could not be fulfilled because it was allocated against the wrong record.",
    ],
    "INV-06": [
        "The balance is {qty} units below zero.",
        "{user_count} users have seen the negative balance in their stock reports.",
        "Order {order_no} could not be allocated because of the negative balance.",
        "The affected stock is valued at about INR {amount}.",
    ],
    "INV-08": [
        "About {qty} units were short against open orders last week.",
        "{user_count} customers have been told to expect a delay.",
        "Order {order_no} was backordered because the stock ran out.",
        "The planners estimate lost sales of about INR {amount}.",
    ],
    "VEN-04": [
        "The contract covers purchases worth about INR {amount} a year.",
        "{qty} open POs would be affected if the contract lapses.",
        "The vendor has not yet shared updated pricing.",
        "Legal has asked for the renewal terms to be sent for review.",
    ],
    "VEN-05": [
        "The first purchase order is worth about INR {amount}.",
        "The planning team has {qty} units waiting to be ordered from this vendor.",
        "The vendor has already been asked to resend documents twice.",
        "The category manager wants this activated within the week.",
    ],
    "VEN-07": [
        "The difference works out to about INR {amount} across open POs.",
        "{qty} units on current invoices are affected.",
        "The buyer has asked the vendor for a written justification.",
        "The vendor says the new rates are effective immediately.",
    ],
    "VEN-08": [
        "{user_count} users from the vendor are locked out.",
        "{qty} advance shipping notices are waiting to be submitted.",
        "Deliveries against open POs may be held at the gate without notices.",
        "The vendor contact has asked for an urgent call.",
    ],
    "CE-03": [
        "The credit they are asking for is about INR {amount}.",
        "{qty} consignments are listed in their complaint.",
        "They have asked for a written response within {hours} hours.",
        "Their account manager has been on two calls with us this week.",
    ],
    "CE-07": [
        "The customer says they have lost about INR {amount} in delayed revenue.",
        "They are asking for a call with our director within {hours} hours.",
        "{user_count} of their staff are waiting to be trained on the platform.",
        "This is the second time they have escalated this quarter.",
    ],
    "CE-08": [
        "{user_count} of their users cannot log in.",
        "They have raised {qty} support tickets from their side.",
        "Their warehouse team cannot book dock slots until they can log in.",
        "The customer asked for a workaround in the meantime.",
    ],
    "CE-11": [
        "They have about {qty} orders waiting to be sent.",
        "Their operations team says every hour of delay costs them about INR {amount}.",
        "They asked whether there is an alternate endpoint they can use.",
        "Their IT team has already checked that the problem is not on their firewall.",
    ],
    "CE-12": [
        "{user_count} of their users have tried to sign in without success.",
        "Their order desk has about {qty} orders waiting to be placed.",
        "They have asked for an update every {hours} hours.",
        "The customer is considering a temporary manual process.",
    ],
    "SEC-02": [
        "{user_count} employees were unable to get in at the start of the shift.",
        "The access log shows {qty} rejected swipes in the last hour.",
        "Facilities has been asked to check the door hardware as well.",
    ],
    "SEC-04": [
        "The change is blocked and a release depends on it.",
        "The requester has chased the approver twice.",
        "{qty} dependent tasks are waiting on the planning board.",
        "The request was raised with a business justification attached.",
    ],
    "SEC-05": [
        "{user_count} users at the site are affected.",
        "The helpdesk has received {qty} calls about it today.",
        "Some users have switched to SMS, which delays login by several minutes.",
        "The issue started right after the weekly firewall maintenance window.",
    ],
    "SEC-06": [
        "The last activity on the account was about {hours} hours ago.",
        "The employee had access to customer data in this system.",
        "HR has confirmed the leaving date.",
        "The access review lists {user_count} other accounts that need checking.",
    ],
    "SEC-07": [
        "The logs show {qty} failed logins followed by a successful one.",
        "{user_count} accounts are involved so far.",
        "The sign-ins came from several countries within a short time.",
        "No data export has been seen yet but monitoring continues.",
    ],
    "SEC-09": [
        "{user_count} users at the site are blocked from the service.",
        "The helpdesk logged {qty} calls about it today.",
        "The change was approved by the network team last week.",
        "Users can still reach the service over the VPN.",
    ],
    "SEC-10": [
        "The account has been used to log in {qty} times in the last month.",
        "The audit lists {user_count} such accounts in total across the estate.",
        "The login history shows no named user for these sessions.",
        "Compliance wants this closed before the next audit.",
    ],
    "SEC-11": [
        "{user_count} staff have reported the problem to the helpdesk.",
        "The first failed login was logged about {hours} hours ago.",
        "Support has handled {qty} calls about the issue.",
        "Users who were already signed in are not affected.",
    ],
    "FAC-01": [
        "Some non-critical servers were shut down to reduce the heat.",
        "{qty} racks are in the affected room.",
        "Portable coolers have been placed as a stopgap.",
        "Facilities has asked the AC contractor to attend urgently.",
    ],
    "FAC-02": [
        "An area of about {qty} square feet is affected.",
        "Some stored goods near the leak had to be moved.",
        "The leak seems worse during heavy rain.",
        "A bucket and a warning sign are in place for now.",
    ],
    "FAC-03": [
        "The dark area covers around {qty} square metres of floor.",
        "{user_count} staff work in that area each shift.",
        "Picking in the affected aisles has slowed down.",
        "The electrician has been asked for a visit today.",
    ],
    "FAC-04": [
        "Each evacuation costs the shift about {hours} minutes.",
        "{user_count} people are evacuated each time.",
        "The alarm panel does not show a clear zone for the trigger.",
        "The fire officer has asked for the detector log.",
    ],
    "FAC-05": [
        "Around {user_count} people use the lift on a normal day.",
        "Deliveries to the upper floors are being carried by hand.",
        "The lift contractor has been called.",
        "A notice has been placed at the entrance.",
    ],
    "FAC-06": [
        "The affected packaging area holds about {qty} cartons.",
        "The pest control contractor has been asked for a visit.",
        "A cleaning team has been asked to inspect the storage.",
        "{user_count} staff work close to the area.",
    ],
    "FAC-07": [
        "{qty} cartons are waiting on the truck.",
        "The truck has been waiting for about {hours} hours.",
        "The maintenance team has been informed.",
        "Other trucks are being redirected to free docks.",
    ],
    "FAC-08": [
        "The outage began about {hours} hours ago.",
        "{qty} cameras are not showing a picture.",
        "Security guards are patrolling the area more often.",
        "The security head has asked for this to be fixed urgently.",
    ],
    "FAC-09": [
        "Around {user_count} people use the area each day.",
        "The contractor has not responded to the earlier reminder.",
        "Consumables have also run out.",
        "Staff have asked for a cleaning crew today.",
    ],
    "FAC-10": [
        "About {qty} vehicles pass the gate each day.",
        "A guard is manually logging each vehicle at the gate.",
        "The barrier arm is propped up for now.",
        "The gate contractor has been asked for a visit.",
    ],
    "FAC-11": [
        "About {user_count} staff were on site when the power failed.",
        "Power has been out for about {hours} hours.",
        "{qty} shipments were waiting for dispatch.",
        "The utility has been called for an estimated restoration time.",
    ],
}

for _s in SCENARIOS:
    _s["extra"].extend(EXTRA_DETAILS.get(_s["id"], []))

SLOT_RE = re.compile(r"\{(\w+)\}")
MIN_DESCRIPTION_VARIANTS = 50


def _slot_card(spec):
    if isinstance(spec, list):
        return len(spec)
    return spec[2] - spec[1] + 1 if spec[0] == "range" else spec[3] - spec[2] + 1


def description_variants(s):
    """Distinct descriptions a template can produce inside a cluster, where the
    anchor, cause and fix slots are held fixed. Mirrors the generator: 1-2 extras
    (in any order) from a pool of 3 or more, otherwise 0 to all of them."""
    fixed = set(s["anchor"]) | set(SLOT_RE.findall(s["cause"] + " " + s["fix"]))
    n = len(s["extra"])
    sizes = (1, 2) if n >= 3 else range(n + 1)
    total = 0
    for k in sizes:
        for picked in itertools.permutations(range(n), k):
            text = " ".join(s["description"] + [s["extra"][i] for i in picked])
            count = 1
            for name in set(SLOT_RE.findall(text)) - fixed:
                count *= _slot_card(SLOTS[name])
            total += count
    return total


def validate():
    """Check slot names, ids, link wiring and per-category template counts."""
    problems = []
    ids = [s["id"] for s in SCENARIOS]
    if len(ids) != len(set(ids)):
        problems.append("duplicate scenario ids")
    by_id = {s["id"]: s for s in SCENARIOS}
    slot_re = re.compile(r"\{(\w+)\}")
    for s in SCENARIOS:
        texts = s["titles"] + s["description"] + s["extra"] + [s["cause"], s["fix"]]
        for text in texts:
            for slot in slot_re.findall(text):
                if slot not in SLOTS:
                    problems.append(f"{s['id']}: unknown slot {{{slot}}}")
        for slot in s["anchor"]:
            if slot not in SLOTS:
                problems.append(f"{s['id']}: unknown anchor {slot}")
        if s["priority"] not in PRIORITY_PROFILES:
            problems.append(f"{s['id']}: bad priority {s['priority']}")
        if len(s["titles"]) != 2:
            problems.append(f"{s['id']}: needs 2 titles")
        # The generator adds at most 2 extras, plus one repeat sentence or
        # near-duplicate prefix, so a ticket stays within 5 sentences.
        if len(s["description"]) < 2 or len(s["description"]) + min(2, len(s["extra"])) > 4:
            problems.append(f"{s['id']}: description/extra sentence count off")
        variants = description_variants(s)
        if variants < MIN_DESCRIPTION_VARIANTS:
            problems.append(f"{s['id']}: only {variants} possible descriptions inside a cluster "
                            f"(want at least {MIN_DESCRIPTION_VARIANTS})")
    for key, link in CROSS_CATEGORY_LINKS.items():
        cats = {by_id[i]["category"] for i in link["scenarios"]}
        if len(cats) != 2:
            problems.append(f"link {key}: must span 2 categories")
        for i in link["scenarios"]:
            if by_id[i]["link"] != key:
                problems.append(f"link {key}: {i} not tagged with link")
    counts = {c: sum(1 for s in SCENARIOS if s["category"] == c) for c in CATEGORIES}
    for c, n in counts.items():
        if not 10 <= n <= 12:
            problems.append(f"{c}: {n} templates (want 10-12)")
    if len(FIRST_NAMES) != 40 or len(LAST_NAMES) != 40:
        problems.append("name pools should be 40 each")
    return problems, counts


if __name__ == "__main__":
    problems, counts = validate()
    print("templates per category:", counts, "total:", len(SCENARIOS))
    print("vague tickets:", len(VAGUE_TICKETS))
    print("problems:", problems or "none")
