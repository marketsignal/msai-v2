#!/usr/bin/env python3
"""Conservative, exact-repository Docker image retention (report-only by default)."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from typing import Any

FULL_ID = re.compile(r'sha256:[0-9a-f]{64}\Z')
REPOSITORY = re.compile(r'[a-z0-9][a-z0-9._/-]*(?::[0-9]+)?(?:/[a-z0-9._/-]+)?\Z')
REF = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9._/:@-]*\Z')


class Refusal(Exception):
    """No authority to mutate the inventory."""


class DockerUnavailable(Exception):
    """Docker is not running; never try to start it."""


def strings(value: Any, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(x, str) or not x for x in value):
        raise Refusal(f'invalid {label}')
    return value


def configuration(path: Path) -> dict[str, Any]:
    data: dict[str, Any] = json.loads(path.read_text())
    for key in ('docker', 'lock_path', 'space_path'):
        if not isinstance(data.get(key), str) or not Path(data[key]).is_absolute():
            raise Refusal(f'{key} must be an absolute path')
    if 'process_probe' in data and (not isinstance(data['process_probe'], str) or not Path(data['process_probe']).is_absolute()):
        raise Refusal('process_probe must be absolute')
    for key in ('context', 'engine_id'):
        if not isinstance(data.get(key), str) or not data[key] or data[key].startswith('-'):
            raise Refusal(f'explicit {key} required')
    if data.get('mode') not in ('local', 'vm'):
        raise Refusal('mode must be local or vm')
    groups = data.get('groups')
    if not isinstance(groups, dict) or not groups:
        raise Refusal('exact repository groups required')
    for name, repos in groups.items():
        if not name or not strings(repos, 'repositories') or any(not REPOSITORY.fullmatch(r) for r in repos):
            raise Refusal('invalid exact repository group')
    pins = strings(data.get('protected_refs', []), 'protected_refs')
    if any(not REF.fullmatch(pin) for pin in pins):
        raise Refusal('invalid protected ref')
    for key, default in (('keep_recent', 3), ('grace_days', 7), ('min_free_bytes', 5 * 1024**3)):
        value = data.setdefault(key, default)
        if type(value) is not int or value < 0:
            raise Refusal(f'invalid {key}')
    return data


class Docker:
    def __init__(self, config: dict[str, Any]):
        self.command = [config['docker'], '--context', config['context']]
        # Explicit --context and a sanitized environment bind every call to this endpoint.
        self.env = {k: v for k, v in os.environ.items() if k not in (
            'DOCKER_HOST', 'DOCKER_CONTEXT', 'DOCKER_TLS_VERIFY', 'DOCKER_CERT_PATH',
            'DOCKER_API_VERSION')}
        self.engine_id = config['engine_id']

    def run(self, *args: str) -> str:
        try:
            result = subprocess.run([*self.command, *args], env=self.env, text=True,
                                    capture_output=True, timeout=120, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise Refusal(f'Docker command unavailable: {args[:2]}') from exc
        if result.returncode:
            if args[0] == 'info' and re.search(
                r'cannot connect|is the docker daemon running|failed to connect.*(?:docker|unix)|dial unix.*(?:connect|no such file)',
                result.stderr, re.IGNORECASE,
            ):
                raise DockerUnavailable
            # No daemon/config credentials or raw inspect output in diagnostics.
            raise Refusal(f'Docker command failed: {" ".join(args[:3])}')
        return result.stdout

    def verify(self, *, initial: bool = False) -> None:
        try:
            info = json.loads(self.run('info', '--format', '{{json .}}'))
        except DockerUnavailable as exc:
            if initial:
                raise
            raise Refusal('Docker became unavailable during retention') from exc
        if not isinstance(info, dict) or info.get('ID') != self.engine_id:
            raise Refusal('Docker daemon ID mismatch')

    def inventory(self) -> tuple[list[dict[str, Any]], set[str]]:
        self.verify()
        image_ids = sorted(set(self.run('image', 'ls', '-aq', '--no-trunc').split()))
        if any(not FULL_ID.fullmatch(i) for i in image_ids):
            raise Refusal('incomplete/invalid image IDs')
        images: list[dict[str, Any]] = []
        # Bound argument size on larger engines.
        for start in range(0, len(image_ids), 100):
            records = json.loads(self.run('image', 'inspect', *image_ids[start:start + 100]))
            if not isinstance(records, list):
                raise Refusal('invalid image inspect')
            images.extend(records)
        if len(images) != len(image_ids) or {i.get('Id') for i in images} != set(image_ids):
            raise Refusal('incomplete image inventory')
        for image in images:
            for key in ('RepoTags', 'RepoDigests'):
                image[key] = strings(image.get(key) or [], key)
                if any(not REF.fullmatch(ref) for ref in image[key]):
                    raise Refusal('malformed image aliases')
        container_ids = sorted(set(self.run('container', 'ls', '-aq', '--no-trunc').split()))
        containers: list[dict[str, Any]] = []
        for start in range(0, len(container_ids), 100):
            records = json.loads(self.run('container', 'inspect', *container_ids[start:start + 100]))
            if not isinstance(records, list):
                raise Refusal('invalid container inspect')
            containers.extend(records)
        if len(containers) != len(container_ids) or {c.get('Id') for c in containers} != set(container_ids):
            raise Refusal('incomplete container inventory')
        used = {c['Image'] for c in containers}
        if any(not FULL_ID.fullmatch(i) for i in used):
            raise Refusal('invalid container image IDs')
        return images, used


def repository(ref: str) -> str:
    name = ref.split('@', 1)[0]
    return name.rsplit(':', 1)[0] if ':' in name.rsplit('/', 1)[-1] else name


def deployment_pins(config: dict[str, Any]) -> set[str]:
    pins = set(strings(config.get('protected_refs', []), 'protected_refs'))
    authorities = config.get('deployment_env', [])
    if config['mode'] == 'vm' and {a.get('name') for a in authorities} != {'current', 'last-good'}:
        raise Refusal('VM requires current and last-good deployment authorities')
    for authority in authorities:
        paths = strings(authority['paths'], 'deployment paths')
        present = [Path(p) for p in paths if Path(p).exists()]
        if not present:
            raise Refusal(f'missing {authority["name"]} deployment authority')
        for path in present:
            fields: dict[str, str] = {}
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith('#'):
                    continue
                match = re.fullmatch(r'([A-Z_][A-Z0-9_]*)=([a-zA-Z0-9._/:@-]+)', line)
                if not match or match[1] in fields:
                    raise Refusal('malformed deployment env (data only; no shell expansion)')
                fields[match[1]] = match[2]
            if fields == {'MSAI_FIRST_DEPLOY': '1'}:
                continue
            keys = ('MSAI_GIT_SHA', 'MSAI_REGISTRY', 'MSAI_BACKEND_IMAGE', 'MSAI_FRONTEND_IMAGE')
            if any(not fields.get(k) for k in keys) or not re.fullmatch(r'[0-9a-f]{7,40}', fields['MSAI_GIT_SHA']):
                raise Refusal('incomplete deployment image authority')
            for key in ('MSAI_BACKEND_IMAGE', 'MSAI_FRONTEND_IMAGE'):
                repo = f'{fields["MSAI_REGISTRY"]}/{fields[key]}'
                if not REPOSITORY.fullmatch(repo):
                    raise Refusal('invalid deployment repository')
                pins.add(f'{repo}:{fields["MSAI_GIT_SHA"]}')
    return pins


def created_at(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        value = re.sub(r'(\.\d{6})\d+', r'\1', value)
        date = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return date.astimezone(timezone.utc) if date.tzinfo else None
    except ValueError:
        return None


def policy(config: dict[str, Any], images: list[dict[str, Any]], used: set[str],
           extra_pins: list[str]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    pins = deployment_pins(config) | set(extra_pins)
    records: list[dict[str, Any]] = []
    dates: dict[str, datetime] = {}
    unknown: set[str] = set()
    for image in images:
        image_id = image['Id']
        tags = image['RepoTags']
        aliases = tags + image['RepoDigests']
        repos = {repository(ref) for ref in aliases}
        membership = {name for name, allowed in config['groups'].items() if repos & set(allowed)}
        owned = len(membership) == 1 and bool(tags) and all(
            repo in config['groups'][next(iter(membership))] for repo in repos)
        known = {repo for allowed in config['groups'].values() for repo in allowed}
        unknown.update(repo for repo in repos - known if 'msai' in repo.lower())
        reasons: list[str] = []
        if not owned:
            reasons.append('unknown-or-mixed-ownership')
        if not tags:
            reasons.append('dangling-or-digest-only')
        if len(tags) > 1:
            reasons.append('multi-tag')
        if image_id in used:
            reasons.append('container-reference')
        if image_id in pins or pins.intersection(aliases):
            reasons.append('protected-pin')
        date = created_at(image.get('Created'))
        if date is None:
            reasons.append('unknown-created')
        else:
            dates[image_id] = date
            if date > now:
                reasons.append('future-created')
            elif now - date < timedelta(days=config['grace_days']):
                reasons.append('creation-grace')
        records.append({'id': image_id, 'tags': tags, 'digests': image['RepoDigests'],
                        'created': image.get('Created'), 'group': next(iter(membership)) if owned else None,
                        'reasons': reasons})
    for group in config['groups']:
        members = [r for r in records if r['group'] == group and r['id'] in dates and dates[r['id']] <= now]
        members.sort(key=lambda r: (dates[r['id']], r['id']), reverse=True)
        for record in members[:config['keep_recent']]:
            record['reasons'].append('recent-reserve')
    return {'images': records, 'candidates': sorted(r['id'] for r in records if not r['reasons']),
            'unapproved_repositories': sorted(unknown), 'pins': sorted(pins)}


def activity(config: dict[str, Any]) -> bool:
    result = subprocess.run([config.get('process_probe', '/bin/ps'), '-axo', 'pid=,command='], capture_output=True,
                            text=True, timeout=15, check=False)
    if result.returncode:
        raise Refusal('cannot inspect local build/deploy activity')
    for line in result.stdout.splitlines():
        if re.search(r'(?:^|/|\s)docker(?:\s+--context\s+\S+)?\s+(?:build\b|buildx\s+(?:build|bake)\b|compose\b.*\s(?:build|pull|up)\b)', line):
            return True
        if re.search(r'(?:^|\s)(?:bash\s+)?\S*deploy-on-vm[^\s]*\.sh(?:\s|$)', line):
            return True
    return False


def metrics(config: dict[str, Any], docker: Docker) -> dict[str, Any]:
    return {'host_filesystem_available_bytes': shutil.disk_usage(config['space_path']).free,
            'space_path': config['space_path'], 'min_free_bytes': config['min_free_bytes'],
            'space_scope': 'host filesystem; not Docker Desktop internal free space',
            'docker_disk_usage': docker.run('system', 'df')}


def execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    config_bytes = args.config.read_bytes()
    config = configuration(args.config)
    if args.min_free_bytes is not None:
        if args.min_free_bytes < 0:
            raise Refusal('negative headroom floor')
        config['min_free_bytes'] = args.min_free_bytes
    if any(not FULL_ID.fullmatch(i) for i in args.approve_id):
        raise Refusal('approve-id requires full sha256 IDs')
    if any(not REF.fullmatch(i) for i in args.pin):
        raise Refusal('invalid prospective pin')
    if args.apply and not (args.approve_id or args.approve_policy):
        raise Refusal('apply requires explicit --approve-id or approved recurring --approve-policy')
    if config['mode'] == 'vm' and args.apply and not args.quiet_deployment:
        raise Refusal('manual VM apply requires current quiet-deployment check')
    lock_path = Path(config['lock_path'])
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {'status': 'SKIP_OVERLAP'}, 2 if config['mode'] == 'vm' else 0
        docker = Docker(config)
        try:
            docker.verify(initial=True)
        except DockerUnavailable:
            return {'status': 'SKIP_DOCKER_UNAVAILABLE'}, 2 if config['mode'] == 'vm' else 0
        if config['mode'] == 'local' and activity(config):
            return {'status': 'SKIP_BUILD_OR_DEPLOY'}, 0
        images, used = docker.inventory()
        report = policy(config, images, used, args.pin)
        report.update({'status': 'REPORT', 'context': config['context'], 'engine_id': config['engine_id'],
                       'before': metrics(config, docker), 'removed': [], 'skipped': []})
        if args.apply:
            approved = set(report['candidates']) if args.approve_policy else set(args.approve_id)
            for image_id in report['candidates']:
                if image_id not in approved:
                    continue
                # Reload config and authorities; never enroll new ownership while applying.
                if args.config.read_bytes() != config_bytes:
                    raise Refusal('configuration changed during retention')
                if config['mode'] == 'local' and activity(config):
                    report['status'] = 'SKIP_BUILD_OR_DEPLOY'
                    break
                fresh_images, fresh_used = docker.inventory()
                fresh = policy(config, fresh_images, fresh_used, args.pin)
                if image_id not in fresh['candidates']:
                    report['skipped'].append(image_id)
                    continue
                try:
                    docker.run('image', 'rm', '--no-prune', image_id)
                except Refusal as exc:
                    report.update({'status': 'REFUSE', 'reason': str(exc), 'failed_id': image_id})
                    report['after'] = metrics(config, docker)
                    return report, 2
                report['removed'].append(image_id)
            else:
                report['status'] = 'APPLIED'
        report['after'] = metrics(config, docker)
        if report['after']['host_filesystem_available_bytes'] < config['min_free_bytes']:
            report['status'] = 'DISK_PRESSURE'
            return report, 3
        return report, 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--approve-id', action='append', default=[])
    parser.add_argument('--approve-policy', action='store_true', help='explicit approval of recurring configured policy')
    parser.add_argument('--pin', action='append', default=[], help='prospective deployment ref to preserve')
    parser.add_argument('--quiet-deployment', action='store_true', help='attest current serialized/quiet VM deployment')
    parser.add_argument('--min-free-bytes', type=int)
    args = parser.parse_args()
    try:
        report, code = execute(args)
    except (Refusal, OSError, ValueError, KeyError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        report, code = {'status': 'REFUSE', 'reason': str(exc), 'deletion_stopped': True}, 2
    print(json.dumps(report, sort_keys=True, indent=2))
    return code


if __name__ == '__main__':
    sys.exit(main())
