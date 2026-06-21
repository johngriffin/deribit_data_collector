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

### Current state (as of 2026-06-21, after archive)

| Resource | Managed by | Notes |
|---|---|---|
| Lambda `rateHandler`, EventBridge schedule, IAM role, log group, deploy bucket | **CloudFormation** (this `serverless.yml`) | `serverless deploy` owns these |
| DynamoDB `deribit_btc` | **External** (created 2022-03-01) | **truncated + recreated empty 2026-06-21**, now **Standard-IA**, PITR on; refilling with mainnet data |
| DynamoDB `deribit_eth` | **External** (created 2022-03-01) | **truncated + recreated empty 2026-06-21**, now **Standard-IA**, PITR on; refilling with mainnet data |
| S3 `deribit-archive-538881967423` | **External** (created 2026-06-21) | **cold archive** of the pre-truncate history; lifecycle → Glacier Deep Archive |
| S3 `deribit-exports` | **External** (created 2024-11-25) | older one-off DynamoDB export snapshot from Nov 2024 (superseded by the archive above) |

The pre-truncate history (continuous hourly snapshots **2022-03-01 → 2026-06-21**, ~54 GB / ~86.6M rows) now lives **only** in the Glacier archive. The live tables restarted empty on 2026-06-21 and grow from there. Schema unchanged: on-demand, single hash key `id` (time-based UUID1), each row has a `timestamp` attribute (epoch ms).

### The archive boundary `T`

- **Archive (Glacier)** holds everything written **before ~2026-06-21 13:38 UTC** (point-in-time of the export; schedule was paused at 13:38 so nothing was written after that until truncation).
- **Live DynamoDB** holds everything from the first run after **13:55 UTC 2026-06-21** onward.
- ⚠️ The archived history before 2026-06-21 is **testnet** data (real index/mark/IV/greeks, but testnet-only OI/volume). Live data from 2026-06-21 onward is **mainnet** (see data-source note at top).

### Cost impact

Before: Standard class, ~54 GB ≈ **~$15–16/mo** (storage-dominated).
After: live tables ~empty in Standard-IA (toward the free tier) + Glacier archive (~12.5 GB compressed ≈ **~$0.15/yr**). Recurring DynamoDB storage cost is now negligible and regrows slowly; one-time export cost was ~$5.40.

### Decision / action log

| Date (UTC) | Action | Status |
|---|---|---|
| 2026-06-21 | Audited stack: tables/exports bucket are **external** to CloudFormation; confirmed testnet data source | done |
| 2026-06-21 | Compared testnet vs mainnet: index/mark/IV/greeks ≈ real, but **OI/volume are testnet-only** and instrument universe differs (1058 vs 934) | done |
| 2026-06-21 | Fixed `handler.py` data source `test.deribit.com` → `www.deribit.com`; verified locally against mainnet | done |
| 2026-06-21 | Deployed fix via `serverless deploy` (stage dev); post-deploy run pulled 934 BTC instruments (mainnet count), no errors | done |
| 2026-06-21 | Created archive bucket `deribit-archive-538881967423` (private, Glacier Deep Archive lifecycle); enabled PITR on both tables | done |
| 2026-06-21 13:38 | Paused schedule (disabled EventBridge rule) to stop writes — guarantees no export→truncate gap | done |
| 2026-06-21 13:54 | Full exports COMPLETED & verified: `deribit_btc` 40,685,169 items / 6.47 GB; `deribit_eth` 45,876,199 items / 6.0 GB. describe-export count == S3 manifest count == ≥ table baseline | done |
| 2026-06-21 13:54 | Truncated both tables (drop + recreate **empty Standard-IA**, PITR re-enabled) — only after export verification passed | done |
| 2026-06-21 13:55 | Re-enabled schedule; collector resumed writing mainnet data to the fresh tables | done |

---

## Apply runbook (safe, ordered — no data loss)

> ✅ **This runbook was executed on 2026-06-21** (see action log above). It is kept here as
> the reference procedure for the **next** archive cycle (when the live tables have regrown
> and you want to roll history off to Glacier again). Do the steps in order; do not skip the
> verification gates. Region is `us-east-1` throughout. The archive was done with the AWS CLI
> (manual), not serverless, because the tables are external to the stack.

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

After the 2026-06-21 trim, history lives in **two places**:

- **Glacier (S3)** — everything up to `T ≈ 2026-06-21 13:38 UTC`. Compressed DynamoDB-JSON, at:
  - `s3://deribit-archive-538881967423/deribit_btc/full-20260621/AWSDynamoDB/01782049146831-e6c5fbce/`
  - `s3://deribit-archive-538881967423/deribit_eth/full-20260621/AWSDynamoDB/01782049148159-b20a6a4a/`
  - (each export folder has a `manifest-summary.json` with the exact `itemCount`)
- **Live DynamoDB** — data from `T` onward (first run 2026-06-21 13:55 UTC; continuously refilled by the Lambda).

To query the full history again:

1. **Restore from Glacier:** `aws s3 restore-object` (or console "Initiate restore") on the export
   objects under the prefixes above. Deep Archive restore takes up to
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
