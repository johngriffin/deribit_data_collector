# Deribit Data Collector

Collects data from Deribit (the leading crypto options exchange).

Pulls all available instruments for BTC and ETH (all strikes and expiries), includes current pricing and full order book.

Runs on Serverless for easy deployment to AWS lambda, storing data in DynamoDB.

> ⚠️ **Data source note:** `handler.py` currently pulls from `https://test.deribit.com`
> (Deribit **testnet**), not `https://www.deribit.com` (mainnet). All historical data
> collected to date is testnet data. If mainnet data was intended, this is a one-line
> fix in `handler.py` — decide before relying on the archive for analysis.

---

## Storage Architecture & Cost Management

### Current state (as of 2026-06-21)

| Resource | Managed by | Notes |
|---|---|---|
| Lambda `rateHandler`, EventBridge schedule, IAM role, log group, deploy bucket | **CloudFormation** (this `serverless.yml`) | `serverless deploy` owns these |
| DynamoDB `deribit_btc` (~26 GB, ~40.7M items) | **External** (created 2022-03-01, not in stack) | referenced by hardcoded ARN in IAM only |
| DynamoDB `deribit_eth` (~28 GB, ~45.9M items) | **External** (created 2022-03-01, not in stack) | referenced by hardcoded ARN in IAM only |
| S3 `deribit-exports` | **External** (created 2024-11-25, not in stack) | holds a one-off DynamoDB export snapshot from Nov 2024 |

Data range: continuous hourly snapshots from **2022-03-01** to present (~54 GB, ~86M rows total).
Both tables are **on-demand (PAY_PER_REQUEST)**, **Standard** table class, no GSIs, single hash key `id` (a time-based UUID1); each row also carries a `timestamp` attribute (epoch ms).

### Cost (Standard class, ~54 GB)

Storage dominates: ~$0.25/GB-mo ≈ **$13.5/mo storage + ~$2/mo writes ≈ ~$15–16/mo**.
(First 25 GB of DynamoDB storage is free account-wide but shared with other tables, so effective cost may be lower.)

### Plan

Two changes, applied in order, to cut cost without changing collector behaviour (the Lambda keeps writing to the same table names — **no `handler.py` change required**):

1. **Standard-IA table class** — storage drops $0.25 → $0.10/GB (~60% off). Keeps data fully queryable. ~$15/mo → ~$8/mo.
2. **Glacier archive + trim** — export the full history to S3 → Glacier Deep Archive (~10 GB compressed ≈ ~$0.12/yr), then trim the cold history out of DynamoDB so the live table stays small and refills going forward. Drives recurring DynamoDB cost toward the free tier.

Net target: **~$15/mo → a few $/mo**, with the full history preserved cheaply in Glacier.

### Decision / action log

| Date | Action | Status |
|---|---|---|
| 2026-06-21 | Audited stack: tables/exports bucket are **external** to CloudFormation; confirmed testnet data source | done |
| 2026-06-21 | Added archive S3 bucket + Glacier Deep Archive lifecycle to `serverless.yml` (IaC) | in PR — not yet deployed |
| 2026-06-21 | Added (commented) DynamoDB table resources with Standard-IA + PITR + Retain + deletion protection, for import-based adoption | in PR — not yet applied |
| _pending_ | Run apply runbook step 1–2 (table class) | **TODO (needs deploy/admin access)** |
| _pending_ | Run apply runbook step 3–6 (export → verify → trim) | **TODO (needs deploy/admin access)** |

---

## Apply runbook (safe, ordered — no data loss)

> None of the steps below have been executed yet. They require AWS admin + Serverless
> dashboard (`serverless login`) credentials. Do them in order; do not skip the
> verification gates. Region is `us-east-1` throughout.

### Step 1 — Make Standard-IA the IaC source of truth (optional but recommended)

The tables predate the stack, so to manage them with serverless you must **import** them
(CloudFormation cannot "create" a table that already exists). The table resource blocks are
in `serverless.yml`, commented out, with `DeletionPolicy: Retain` + `DeletionProtectionEnabled: true`
so an import/rollback can never delete data.

1. Uncomment the `DeribitBtcTable` / `DeribitEthTable` resources in `serverless.yml`.
2. Import the existing tables into the stack (AWS Console → CloudFormation → stack
   `deribit-data-collector-dev` → Stack actions → **Import resources into stack**, or a
   CLI `IMPORT`-type change set). Match logical IDs to the resource names above.
3. `serverless deploy` — this applies `TableClass: STANDARD_INFREQUENT_ACCESS` and PITR to
   the now-managed tables.

**Shortcut (if you accept tables staying external):** skip the import and set the class directly —
```
aws dynamodb update-table --region us-east-1 --table-name deribit_btc --table-class STANDARD_INFREQUENT_ACCESS
aws dynamodb update-table --region us-east-1 --table-name deribit_eth --table-class STANDARD_INFREQUENT_ACCESS
```
This is the only "manual" step; it does not cause drift because the tables are not in the stack.
Record it in the action log above if you use it.

### Step 2 — Deploy the archive bucket

`serverless deploy` creates the `DeribitArchiveBucket` (Glacier Deep Archive lifecycle). Safe,
additive — it creates only a new bucket.

### Step 3 — Enable PITR (required for DynamoDB → S3 export)

```
aws dynamodb update-continuous-backups --region us-east-1 --table-name deribit_btc \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true
aws dynamodb update-continuous-backups --region us-east-1 --table-name deribit_eth \
  --point-in-time-recovery-specification PointInTimeRecoveryEnabled=true
```
(If the table resources were imported in Step 1, PITR is already on via `serverless deploy`.)

### Step 4 — Full export to S3 (non-destructive)

```
aws dynamodb export-table-to-point-in-time --region us-east-1 \
  --table-arn arn:aws:dynamodb:us-east-1:538881967423:table/deribit_btc \
  --s3-bucket deribit-archive-538881967423 --s3-prefix deribit_btc/full-$(date +%Y%m%d) \
  --export-format DYNAMODB_JSON
# repeat for deribit_eth
```
Note the returned `ExportArn`. Lifecycle moves the objects to Deep Archive automatically.

### Step 5 — VERIFY the export before deleting anything (gate)

```
aws dynamodb describe-export --region us-east-1 --export-arn <ExportArn>
# require: ExportStatus=COMPLETED, ItemCount ≈ table item count, no FailureCode
```
Confirm `ItemCount` matches `aws dynamodb describe-table ... Table.ItemCount` (±recent writes).
**Do not proceed until both exports are COMPLETED and counts reconcile.**

### Step 6 — Trim DynamoDB (destructive — only after Step 5 passes)

Pick ONE:

- **Keep recent, drop old (recommended):** delete only items older than your retention window
  (e.g. keep last 6 months) using a paginated scan on `timestamp` + `BatchWriteItem` deletes.
  Deletes cost write units (~$1.25/M items) — budget for it.
- **Full wipe + refill (cheapest, biggest blast radius):** capture an **incremental** export
  covering the window since the Step 4 export (so nothing written in between is lost), verify it,
  then `delete-table` + recreate empty with the same name and `STANDARD_INFREQUENT_ACCESS`.
  The Lambda refills from empty with no code change.
  ⚠️ If tables were imported into CF (Step 1), disable deletion protection and remove them from the
  stack with `DeletionPolicy: Retain` first, or CF will fight the recreate. Simpler to do a full
  wipe **before** importing.

**Avoiding the gap:** rows written between the Step 4 export and the trim are NOT in the full
export. Always run an incremental export (or pause the schedule for the ~10-min window) immediately
before a full wipe so no hourly sample is lost.

---

## Reconciling the two storages later

After trimming, history lives in **two places**:

- **Glacier (S3)** — everything up to the export/trim time `T`. Compressed DynamoDB-JSON.
- **Live DynamoDB** — data from `T` onward (continuously refilled by the Lambda).

To query the full history again:

1. **Restore from Glacier:** `aws s3 restore-object` (or console "Initiate restore") on the export
   objects under `deribit-archive-538881967423/deribit_<coin>/...`. Deep Archive restore takes up to
   ~12 h (bulk) / ~48 h depending on tier; budget for it.
2. **Query the archive without reloading DynamoDB (preferred):** point **Athena** at the restored
   export prefix. DynamoDB export JSON is one row per item; create an external table over it and
   query by `timestamp`. Much cheaper than re-importing 50 GB into DynamoDB.
3. **Or re-import to a temp table:** `aws dynamodb import-table` from the restored S3 prefix into a
   new table (e.g. `deribit_btc_archive`) — do **not** import back into the live table.
4. **Union the windows:** archive covers `t < T`, live table covers `t >= T`. De-dup on `id` if your
   trim/export windows overlapped (overlap is safe; gaps are not — see "Avoiding the gap" above).
   `id` is a UUID1 so it also encodes time if you need to derive `T` per row.

Keep the `T` boundary (export timestamp) recorded in the action log each time you archive, so future
reconciliation knows exactly where live data starts.

---

## Usage

### Configuration

Update handler.py and serverless.yml with the appropriate DynamoDB ids, serverless will automatically deal with IAM roles and permissions.

### Deployment

This example is made to work with the Serverless Framework dashboard, which includes advanced features such as CI/CD, monitoring, metrics, etc.

In order to deploy with dashboard, you need to first login with:

```
serverless login
```

and then perform deployment with:

```
serverless deploy
```

After running deploy, you should see output similar to:

```bash
Deploying aws-python-scheduled-cron-project to stage dev (us-east-1)

✔ Service deployed to stack aws-python-scheduled-cron-project-dev (205s)

functions:
  rateHandler: aws-python-scheduled-cron-project-dev-rateHandler (2.9 kB)
  cronHandler: aws-python-scheduled-cron-project-dev-cronHandler (2.9 kB)
```

There is no additional step required. Your defined schedules becomes active right away after deployment.

### Local invocation

In order to test out your functions locally, you can invoke them with the following command:

```
serverless invoke local --function rateHandler
```

After invocation, you should see output similar to:

```bash
INFO:handler:Your cron function aws-python-scheduled-cron-dev-rateHandler ran at 15:02:43.203145
```
</content>
</invoke>
