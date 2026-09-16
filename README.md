# VMware Live Site Recovery Prometheus Exporter

A small Prometheus exporter for VMware Live Site Recovery (formerly SRM) and
vSphere Replication. It is designed to answer the operational questions that
matter during normal operations: are replications healthy, are RPOs being
breached, when did a VM last sync, are there open SRM issues, and have recovery
plans recently failed?

## What it collects

For every configured vSphere Replication endpoint, the exporter collects VM
replication status, configured RPO, current RPO violation, last sync time,
duration and bytes, sync progress/bytes, configured options, point-in-time
snapshot count and retention, and reported configuration/group/recovery errors.
It also exposes active replication issues. For Live Site Recovery/SRM it
collects pairing, SRM, protection-group, and recovery-plan issues plus recovery
plan execution history.

The exact available fields vary by VMware version and replication type. Array
based replication normally does not expose per-VM RPO through vSphere
Replication; use the storage vendor's exporter alongside this one for that
authoritative data.

## Quick start

Copy `config.example.yml` to `config.yml`, set the endpoint(s), and provide
credentials through environment variables:

```sh
export VSR_PRIMARY_PASSWORD='replace-me'
docker build -t vmware-lsr-exporter:local .
docker run --rm -p 9828:9828 \
  -v "$PWD/config.yml:/etc/vsr-exporter/config.yml:ro" \
  -e VSR_PRIMARY_PASSWORD \
  vmware-lsr-exporter:local
```

Prometheus scrapes `http://exporter:9828/metrics`. The exporter refreshes data
in the background, so Prometheus requests do not wait for VMware API calls.

## Configuration

See [config.example.yml](config.example.yml). Each `instances` item has its own
endpoint, credentials, API base path, TLS policy, and endpoint toggles.
`${NAME}` and `${NAME:-default}` are expanded from the container environment.
Use Docker/Kubernetes secrets to populate passwords; do not commit them.

The replication and SRM REST gateways are distinct. For vSphere Replication,
use the VR appliance and `api_base: /api/rest/vr/v1`; this yields VM RPO and
sync metrics. For Live Site Recovery/SRM workflow health, use the SRM/VLSR
appliance and `api_base: /api/rest/srm/v1`. Configure them as two instances
when they are separate appliances. The login endpoint defaults to `/session`.

The API user needs read-only permissions sufficient to list pairings,
replications, issues, protection groups, recovery plans, and plan history.

## Core metrics

* `vmware_vsr_replication_status` — one sample per VM/status value; `1` is
  the current reported state.
* `vmware_vsr_replication_rpo_violation` and
  `vmware_vsr_replication_current_rpo_violation_seconds`.
* `vmware_vsr_replication_configured_rpo_seconds`,
  `vmware_vsr_replication_last_sync_timestamp_seconds`,
  `vmware_vsr_replication_last_sync_duration_seconds`, and
  `vmware_vsr_replication_last_sync_bytes`.
* `vmware_vsr_replication_sync_bytes_{current,total}`, sync-progress ratio,
  configured point-in-time snapshot count, and snapshot retention.
* `vmware_vsr_issue_active` and `vmware_lsr_issue_active`.
* `vmware_lsr_recovery_plan_run` — one sample for each returned execution
  record, labeled with plan and outcome. Use it for recent run/failure views;
  do not treat it as a counter.
* `vmware_lsr_task_status` — recent Live Site Recovery task status, including
  failed recovery, reprotect, test, and cleanup operations when returned by the
  appliance.
* `vmware_lsr_exporter_last_success_timestamp_seconds` and
  `vmware_lsr_exporter_scrape_error` identify an exporter/API problem.

Error text, task IDs, and detailed issue messages are intentionally not labels:
they are high-cardinality and potentially sensitive. Use the VMware UI/API logs
to investigate an active issue after an alert fires.

## Alerts

Example alert rules are in [alerts.yml](alerts.yml). In particular, alert on an
RPO breach, stale syncs relative to configured RPO, active error-severity
issues, recovery-plan failures, and exporter scrape failures.

## API references

This exporter uses the supported session-based REST APIs. VMware documents
`GET /pairings/{pairing_id}/replications?extended_info=true` as exposing
`rpo`, RPO violation, sync and recovery fields, and documents separate issue
and recovery-history endpoints. VMware returns RPO and current RPO overage in
minutes; the exporter converts both to seconds. Confirm the exact paths and privileges for the
Live Site Recovery/vSphere Replication versions installed in your environment.
