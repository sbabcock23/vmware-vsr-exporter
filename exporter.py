#!/usr/bin/env python3
"""Prometheus exporter for VMware Live Site Recovery and vSphere Replication."""
import argparse
import datetime as dt
import logging
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import requests
import yaml
from prometheus_client import Gauge, start_http_server
from prometheus_client.core import GaugeMetricFamily, REGISTRY

LOG = logging.getLogger("vmware_lsr_exporter")
ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    if isinstance(value, str):
        return ENV.sub(lambda m: os.getenv(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def truth(value: Any) -> float:
    return 1.0 if value is True or value in (1, "1", "true", "True") else 0.0


def number(value: Any, default: float = 0) -> float:
    try:
        return float(value) if value is not None else default
    except (ValueError, TypeError):
        return default


def unix_seconds(value: Any) -> float:
    if isinstance(value, str) and not value.isdigit():
        try:
            return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0
    value = number(value)
    # VMware API timestamps are commonly Unix milliseconds.
    return value / 1000 if value > 10_000_000_000 else value


@dataclass
class Instance:
    name: str
    url: str
    username: str
    password: str
    api_base: str = "/api/rest/vr/v1"
    login_path: str = "/session"
    verify_tls: Any = True
    timeout: int = 20
    collect_replications: bool = True
    collect_issues: bool = True
    collect_lsr_health: bool = True
    history_days: int = 7
    expected_vms: list[str] = field(default_factory=list)


class VMwareClient:
    def __init__(self, config: Instance):
        self.config = config
        self.http = requests.Session()
        self.http.verify = config.verify_tls
        self.base = config.url.rstrip("/") + config.api_base.rstrip("/")

    def login(self) -> None:
        response = self.http.post(self.base + self.config.login_path,
                                  auth=(self.config.username, self.config.password),
                                  headers={"Accept": "application/json", "Content-Type": "application/json"},
                                  timeout=self.config.timeout)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError:
            data = response.text.strip().strip('"')
        token = data.get("session_id") if isinstance(data, dict) else data
        token = token or response.headers.get("x-dr-session")
        if not token:
            raise RuntimeError("login response did not contain an x-dr-session token")
        self.http.headers["x-dr-session"] = token

    def get(self, path: str, **params: Any) -> Any:
        response = self.http.get(self.base + path, params={k: v for k, v in params.items() if v is not None},
                                 timeout=self.config.timeout)
        response.raise_for_status()
        return response.json()

    def list(self, path: str, **params: Any) -> list[dict]:
        result = self.get(path, **params)
        return result.get("list", []) if isinstance(result, dict) else result


class Collector:
    def __init__(self, configs: list[Instance]):
        self.configs = configs
        self.samples: list[tuple[str, list[str], float]] = []
        self.lock = threading.Lock()
        self.last_success: dict[str, float] = {}
        self.errors: dict[str, int] = {c.name: 0 for c in configs}
        # Kept in-memory to identify an initial/full sync that has stopped making progress.
        self.sync_progress_seen: dict[tuple[str, str, str], tuple[tuple[str, float, float], float]] = {}

    def describe(self):
        return []

    def add(self, metric: str, labels: list[str], value: Any) -> None:
        self.samples.append((metric, labels, number(value)))

    def collect(self):
        with self.lock:
            samples, last_success, errors = list(self.samples), dict(self.last_success), dict(self.errors)
        families = {
            "replication_status": GaugeMetricFamily("vmware_vsr_replication_status", "Current VMware replication status (one current status sample is 1).", labels=["instance", "pairing", "vm", "status"]),
            "replication_rpo_violation": GaugeMetricFamily("vmware_vsr_replication_rpo_violation", "Whether configured RPO is currently violated.", labels=["instance", "pairing", "vm"]),
            "replication_current_rpo_violation_seconds": GaugeMetricFamily("vmware_vsr_replication_current_rpo_violation_seconds", "Current RPO violation duration.", labels=["instance", "pairing", "vm"]),
            "replication_configured_rpo_seconds": GaugeMetricFamily("vmware_vsr_replication_configured_rpo_seconds", "Configured RPO.", labels=["instance", "pairing", "vm"]),
            "replication_last_sync_timestamp_seconds": GaugeMetricFamily("vmware_vsr_replication_last_sync_timestamp_seconds", "Last successful sync timestamp.", labels=["instance", "pairing", "vm"]),
            "replication_last_sync_duration_seconds": GaugeMetricFamily("vmware_vsr_replication_last_sync_duration_seconds", "Duration of latest sync.", labels=["instance", "pairing", "vm"]),
            "replication_last_sync_bytes": GaugeMetricFamily("vmware_vsr_replication_last_sync_bytes", "Bytes transferred in latest sync.", labels=["instance", "pairing", "vm"]),
            "replication_sync_bytes_current": GaugeMetricFamily("vmware_vsr_replication_sync_bytes_current", "Current sync transferred bytes.", labels=["instance", "pairing", "vm"]),
            "replication_sync_bytes_total": GaugeMetricFamily("vmware_vsr_replication_sync_bytes_total", "Current sync total bytes.", labels=["instance", "pairing", "vm"]),
            "replication_snapshots": GaugeMetricFamily("vmware_vsr_replication_snapshots", "Configured point-in-time snapshot instances.", labels=["instance", "pairing", "vm"]),
            "replication_snapshot_retention_days": GaugeMetricFamily("vmware_vsr_replication_snapshot_retention_days", "Configured point-in-time snapshot retention.", labels=["instance", "pairing", "vm"]),
            "replication_sync_progress_ratio": GaugeMetricFamily("vmware_vsr_replication_sync_progress_ratio", "Current replication sync progress from 0 to 1.", labels=["instance", "pairing", "vm"]),
            "replication_initial_sync_active": GaugeMetricFamily("vmware_vsr_replication_initial_sync_active", "Whether a VM is in its initial full synchronization.", labels=["instance", "pairing", "vm"]),
            "replication_full_sync_active": GaugeMetricFamily("vmware_vsr_replication_full_sync_active", "Whether a VM is performing an initial or subsequent full synchronization.", labels=["instance", "pairing", "vm"]),
            "replication_sync_progress_last_change_timestamp_seconds": GaugeMetricFamily("vmware_vsr_replication_sync_progress_last_change_timestamp_seconds", "When observed sync state, transferred bytes, or progress last changed; resets when exporter restarts.", labels=["instance", "pairing", "vm"]),
            "replication_option_enabled": GaugeMetricFamily("vmware_vsr_replication_option_enabled", "Whether a replication option is enabled.", labels=["instance", "pairing", "vm", "option"]),
            "expected_vm_protected": GaugeMetricFamily("vmware_vsr_expected_vm_protected", "Whether an expected VM is currently discovered as protected by vSphere Replication.", labels=["instance", "vm"]),
            "expected_vms": GaugeMetricFamily("vmware_vsr_expected_vms", "Configured expected VM count.", labels=["instance"]),
            "expected_vms_protected": GaugeMetricFamily("vmware_vsr_expected_vms_protected", "Expected VM count currently discovered as protected.", labels=["instance"]),
            "replication_error": GaugeMetricFamily("vmware_vsr_replication_error", "Replication reports configuration, group, or recovery error.", labels=["instance", "pairing", "vm", "type"]),
            "vsr_issue_active": GaugeMetricFamily("vmware_vsr_issue_active", "Active vSphere Replication issue.", labels=["instance", "pairing", "severity", "issue_type"]),
            "lsr_issue_active": GaugeMetricFamily("vmware_lsr_issue_active", "Active Live Site Recovery issue.", labels=["instance", "pairing", "entity_type", "severity", "issue_type"]),
            "recovery_plan_run": GaugeMetricFamily("vmware_lsr_recovery_plan_run", "Recovery plan execution record (not a counter).", labels=["instance", "pairing", "plan", "result", "operation"]),
            "recovery_plan_last_run_timestamp_seconds": GaugeMetricFamily("vmware_lsr_recovery_plan_last_run_timestamp_seconds", "Start time of a returned recovery plan execution.", labels=["instance", "pairing", "plan", "result", "operation"]),
            "task_status": GaugeMetricFamily("vmware_lsr_task_status", "Recent Live Site Recovery task state (one current status sample is 1).", labels=["instance", "pairing", "entity_type", "status"]),
            "exporter_last_success_timestamp_seconds": GaugeMetricFamily("vmware_lsr_exporter_last_success_timestamp_seconds", "Last successful collection time.", labels=["instance"]),
            "exporter_scrape_error": GaugeMetricFamily("vmware_lsr_exporter_scrape_error", "Latest collection failed.", labels=["instance"]),
        }
        for metric, labels, value in samples:
            families[metric].add_metric(labels, value)
        for name in errors:
            families["exporter_scrape_error"].add_metric([name], errors[name])
            families["exporter_last_success_timestamp_seconds"].add_metric([name], last_success.get(name, 0))
        yield from families.values()

    def refresh(self) -> None:
        new_samples: list[tuple[str, list[str], float]] = []
        for config in self.configs:
            self.samples = new_samples
            try:
                self.refresh_instance(config)
                self.last_success[config.name] = time.time()
                self.errors[config.name] = 0
            except Exception as exc:  # retain data from other instances
                LOG.warning("collection failed for %s: %s", config.name, exc)
                self.errors[config.name] = 1
        with self.lock:
            self.samples = new_samples

    def refresh_instance(self, config: Instance) -> None:
        client = VMwareClient(config)
        client.login()
        pairings = client.list("/pairings")
        protected_vms: set[str] = set()
        for pairing in pairings:
            pairing_id = str(pairing.get("id", pairing.get("pairing_id", "unknown")))
            pairing_name = str(pairing.get("name", pairing_id))
            if config.collect_replications:
                for replication in client.list(f"/pairings/{pairing_id}/replications", extended_info="true", limit=1000):
                    self.replication(config.name, pairing_name, replication)
                    protected_vms.add(str(replication.get("name", "")))
            if config.collect_issues:
                self.issues(client, config.name, pairing_id, pairing_name, "/replications/issues", "vsr_issue_active", "replication")
            if config.collect_lsr_health:
                self.lsr_health(client, config, pairing_id, pairing_name)
        if config.expected_vms:
            protected_expected = 0
            for vm in config.expected_vms:
                is_protected = vm in protected_vms
                protected_expected += int(is_protected)
                self.add("expected_vm_protected", [config.name, vm], truth(is_protected))
            self.add("expected_vms", [config.name], len(config.expected_vms))
            self.add("expected_vms_protected", [config.name], protected_expected)

    def replication(self, instance: str, pairing: str, item: dict) -> None:
        vm = str(item.get("name", item.get("vm_id", item.get("id", "unknown"))))
        status = item.get("status", {}) or {}
        # Current APIs use a compound object, but accepting a plain status value
        # makes this resilient to API-version differences.
        state_value = status.get("status") if isinstance(status, dict) else status
        state = str(state_value or item.get("configuration_state", "unknown")).upper()
        labels = [instance, pairing, vm]
        self.add("replication_status", labels + [state], 1)
        self.add("replication_rpo_violation", labels, truth(status.get("rpo_violation") if isinstance(status, dict) else False))
        # The VMware API expresses both RPO fields in minutes.
        self.add("replication_current_rpo_violation_seconds", labels, number(item.get("current_rpo_violation")) * 60)
        self.add("replication_configured_rpo_seconds", labels, number(item.get("rpo")) * 60)
        self.add("replication_last_sync_timestamp_seconds", labels, unix_seconds(item.get("last_sync_time")))
        self.add("replication_last_sync_duration_seconds", labels, item.get("last_sync_duration"))
        self.add("replication_last_sync_bytes", labels, item.get("last_sync_size"))
        sync = item.get("sync_progress", {}) or {}
        self.add("replication_sync_bytes_current", labels, sync.get("transferred_current"))
        self.add("replication_sync_bytes_total", labels, sync.get("transferred_total"))
        self.add("replication_sync_progress_ratio", labels, number(sync.get("progress")) / 100)
        self.add("replication_initial_sync_active", labels, truth(state == "INITIAL_FULL_SYNC"))
        self.add("replication_full_sync_active", labels, truth(state in ("INITIAL_FULL_SYNC", "FULL_SYNC")))
        progress_key = (instance, pairing, vm)
        progress_value = (state, number(sync.get("progress")), number(sync.get("transferred_current")))
        previous = self.sync_progress_seen.get(progress_key)
        changed_at = time.time() if previous is None or previous[0] != progress_value else previous[1]
        self.sync_progress_seen[progress_key] = (progress_value, changed_at)
        self.add("replication_sync_progress_last_change_timestamp_seconds", labels, changed_at)
        self.add("replication_snapshots", labels, item.get("mpit_instances"))
        self.add("replication_snapshot_retention_days", labels, item.get("mpit_days"))
        for option in ("quiescing_enabled", "network_compression_enabled", "encryption_enabled",
                       "auto_replicate_new_disks_enabled", "mpit_enabled", "vm_data_sets_replication_enabled",
                       "enhanced_replication"):
            self.add("replication_option_enabled", labels + [option], truth(item.get(option)))
        for key in ("configuration_error", "last_group_error", "recovery_error"):
            if item.get(key):
                self.add("replication_error", labels + [key], 1)

    def issues(self, client: VMwareClient, instance: str, pairing_id: str, pairing: str, path: str, metric: str, entity: str) -> None:
        try:
            items = client.list(f"/pairings/{pairing_id}{path}", limit=1000)
        except requests.HTTPError as exc:
            if exc.response.status_code == 404:
                return
            raise
        for issue in items:
            severity = str(issue.get("status", "unknown"))
            issue_type = str(issue.get("issue_type", "unknown"))
            if metric == "vsr_issue_active":
                self.add(metric, [instance, pairing, severity, issue_type], 1)
            else:
                self.add(metric, [instance, pairing, entity, severity, issue_type], 1)

    def lsr_health(self, client: VMwareClient, config: Instance, pairing_id: str, pairing: str) -> None:
        for path, entity in (("/issues", "pairing"),):
            self.issues(client, config.name, pairing_id, pairing, path, "lsr_issue_active", entity)
        try:
            for task in client.list("/tasks", limit=1000):
                entity = str(task.get("entity", task.get("entity_name", "unknown"))).split(":", 1)[0]
                status = str(task.get("status", "unknown"))
                self.add("task_status", [config.name, pairing, entity, status], 1)
        except requests.HTTPError as exc:
            if exc.response.status_code != 404:
                raise
        try:
            groups = client.list(f"/pairings/{pairing_id}/protection-management/groups", limit=1000)
            plans = client.list(f"/pairings/{pairing_id}/recovery-management/plans", limit=1000)
        except requests.HTTPError as exc:
            # Some VR-only deployments expose replication but not the SRM inventory API.
            if exc.response.status_code == 404:
                return
            raise
        for group in groups:
            gid, name = str(group.get("id")), str(group.get("name", group.get("id")))
            self.issues(client, config.name, pairing_id, pairing, f"/protection-management/groups/{gid}/issues", "lsr_issue_active", f"protection_group:{name}")
        for plan in plans:
            pid, name = str(plan.get("id")), str(plan.get("name", plan.get("id")))
            self.issues(client, config.name, pairing_id, pairing, f"/recovery-management/plans/{pid}/issues", "lsr_issue_active", f"recovery_plan:{name}")
            try:
                history = client.list(f"/pairings/{pairing_id}/recovery-management/plans/{pid}/history-reports",
                                      limit=1000,
                                      start_date=int((time.time() - config.history_days * 86400) * 1000),
                                      end_date=int(time.time() * 1000))
            except requests.HTTPError as exc:
                if exc.response.status_code == 404:
                    continue
                raise
            for run in history:
                nested_result = run.get("result", {}) or {}
                result = str(run.get("status") or (nested_result.get("status") if isinstance(nested_result, dict) else None) or "unknown")
                operation = str(run.get("operation", run.get("type", "unknown")))
                self.add("recovery_plan_run", [config.name, pairing, name, result, operation], 1)
                self.add("recovery_plan_last_run_timestamp_seconds", [config.name, pairing, name, result, operation],
                         unix_seconds(run.get("start_time", run.get("queued_time"))))


def load_config(filename: str) -> tuple[list[Instance], dict]:
    with open(filename, encoding="utf-8") as handle:
        raw = expand_env(yaml.safe_load(handle) or {})
    default_timeout = int(raw.get("request_timeout_seconds", 20))
    configs = []
    for data in raw.get("instances", []):
        if not data.get("password"):
            raise ValueError(f"instance {data.get('name', '<unnamed>')} has an empty password")
        expected_vms = data.get("expected_vms", [])
        if not isinstance(expected_vms, list) or not all(isinstance(vm, str) and vm for vm in expected_vms):
            raise ValueError(f"instance {data.get('name', '<unnamed>')} expected_vms must be a list of non-empty VM names")
        if len(expected_vms) != len(set(expected_vms)):
            raise ValueError(f"instance {data.get('name', '<unnamed>')} expected_vms contains duplicate VM names")
        if data.get("ca_bundle"):
            data["verify_tls"] = data["ca_bundle"]
        data.setdefault("timeout", default_timeout)
        configs.append(Instance(**{k: v for k, v in data.items() if k in Instance.__dataclass_fields__}))
    if not configs:
        raise ValueError("configuration needs at least one instance")
    return configs, raw


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    configs, raw = load_config(args.config)
    collector = Collector(configs)
    REGISTRY.register(collector)
    Gauge("vmware_lsr_exporter_build_info", "Exporter build information", ["version"]).labels("0.1.0").set(1)
    start_http_server(int(raw.get("listen_port", 9828)), addr=raw.get("listen_address", "0.0.0.0"))
    interval = max(10, int(raw.get("refresh_interval_seconds", 60)))
    while True:
        collector.refresh()
        time.sleep(interval)


if __name__ == "__main__":
    main()
