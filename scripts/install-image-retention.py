#!/usr/bin/env python3
"""Preview a stable Mac LaunchAgent installation, or print the VM policy config."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile
from typing import Any

LABEL = 'ai.marketsignal.image-retention'
LOCAL_REPOSITORIES = [
    'msai-backend', 'msai-frontend', 'msai-v2-research-baseline', 'msai-v2-research-candidate',
    'msai-t2p-01a114db', 'msai-t4-genuine-20261008', 'msai-t5-controls-01a11f5c',
    'msai-t5-selection-01a11f56', 'msai-t7-finance-01a12206', 'msai-t7a-01a1227e',
    'msai-t10b-01a12c43', 'msai-t10b-01a12ca9',
]
LOCAL_PINS = [
    'msai-v2-research-baseline:9d51a63', 'msai-v2-research-candidate:rc6-wip',
    'msai-t4-genuine-20261008:r7', 'msai-t5-selection-01a11f56:r18',
    'msai-t10b-01a12c43:runtime', 'msai-t10b-01a12c43:controls',
    'msai-t10b-01a12ca9:qualified-base', 'msai-t10b-01a12ca9:controls',
    'msai-t10b-01a12ca9:report-projection',
]
VM_REGISTRY = 'msaiacr4cd6d2obcxqaa.azurecr.io'


def default_config(target: str, stable: Path) -> dict[str, Any]:
    if target == 'vm':
        return {
            'mode': 'vm', 'docker': '/usr/bin/docker', 'context': 'default',
            'engine_id': 'ef8cfd31-0169-48c7-aba9-6e87f5ab82ba',
            'groups': {part: [f'{VM_REGISTRY}/msai-{part}'] for part in ('backend', 'frontend')},
            'protected_refs': [],
            'deployment_env': [
                {'name': 'current', 'paths': ['/run/msai-images.env', '/opt/msai/msai-images.env']},
                {'name': 'last-good', 'paths': ['/run/msai-images.last-good.env', '/opt/msai/msai-images.last-good.env']},
            ],
            'lock_path': '/run/msai-image-retention.lock', 'space_path': '/',
            'keep_recent': 0, 'grace_days': 7, 'min_free_bytes': 5 * 1024**3,
        }
    return {
        'mode': 'local', 'docker': str(stable.parents[3] / '.docker/bin/docker'), 'context': 'desktop-linux',
        'engine_id': '3bd81cf7-cf61-47f6-9d7d-90927da5eb24',
        'groups': {'local-msai': LOCAL_REPOSITORIES}, 'protected_refs': LOCAL_PINS,
        'lock_path': str(stable / 'retention.lock'), 'space_path': str(stable.parents[3]),
        'keep_recent': 0, 'grace_days': 7, 'min_free_bytes': 5 * 1024**3,
    }


def merge(config: dict[str, Any], update: dict[str, Any]) -> dict[str, Any]:
    """Preserve operator additions when updating defaults/installed policy."""
    pins = sorted(set(config['protected_refs']) | set(update.get('protected_refs', [])))
    groups = {key: list(value) for key, value in config['groups'].items()}
    for key, values in update.get('groups', {}).items():
        groups[key] = sorted(set(groups.get(key, [])) | set(values))
    config.update(update)
    config['groups'] = groups
    config['protected_refs'] = pins
    return config


def atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_bytes(content)
    temporary.chmod(0o600)
    temporary.replace(path)


def native(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(['/bin/launchctl', *args], text=True, capture_output=True,
                          check=check, timeout=30)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--config', type=Path, help='reviewed config overrides; pins are merged')
    parser.add_argument('--python', type=Path, default=Path(sys.executable))
    parser.add_argument('--target', choices=('mac', 'vm'), default='mac')
    parser.add_argument('--apply', action='store_true', help='install recurring policy after operational approval')
    parser.add_argument('--status', action='store_true')
    args = parser.parse_args()
    args.home = args.home.absolute()
    stable = args.home / 'Library/Application Support/MSAI/image-retention'
    config_path = stable / 'config.json'
    plist_path = args.home / 'Library/LaunchAgents' / f'{LABEL}.plist'
    domain = f'gui/{os.getuid()}'
    if args.status:
        result = native('print', f'{domain}/{LABEL}', check=False)
        print(json.dumps({'status': 'LOADED' if result.returncode == 0 else 'NOT_LOADED',
                          'native_status': result.stdout or result.stderr,
                          'config_path': str(config_path), 'plist_path': str(plist_path)}))
        return 0 if result.returncode == 0 else 2
    config = default_config(args.target, stable)
    if config_path.exists() and args.target == 'mac':
        config = merge(config, json.loads(config_path.read_text()))
    if args.config:
        config = merge(config, json.loads(args.config.read_text()))
    python = args.python.absolute()
    helper = stable / 'docker-image-retention.py'
    if not python.is_file() or not os.access(python, os.X_OK) or '.worktrees' in str(python):
        raise ValueError('Python must be an absolute executable independent of removable worktrees')
    payload: dict[str, Any] = {
        'Label': LABEL, 'ProgramArguments': [str(python), str(helper), '--config', str(config_path),
                                            '--apply', '--approve-policy'],
        'StartCalendarInterval': {'Hour': 3, 'Minute': 15}, 'RunAtLoad': False,
        'StandardOutPath': str(stable / 'stdout.log'), 'StandardErrorPath': str(stable / 'stderr.log'),
        'WorkingDirectory': str(stable),
    }
    preview = {'status': 'PREVIEW', 'config': config, 'config_path': str(config_path),
               'helper_path': str(helper), 'plist_path': str(plist_path),
               'plist': plistlib.dumps(payload).decode() if args.target == 'mac' else None}
    if not args.apply:
        print(json.dumps(preview, indent=2, sort_keys=True))
        return 0
    if args.target != 'mac' or platform.system() != 'Darwin':
        raise ValueError('automatic installation is Mac-only; VM has no retention timer')
    if any('.worktrees' in str(p) for p in (stable, config_path, python)):
        raise ValueError('installation paths must survive worktree removal')
    docker_path = Path(config['docker'])
    if not docker_path.is_absolute() or not os.access(docker_path, os.X_OK):
        raise ValueError('config requires an absolute executable Docker path')
    # The operator must see the actual report before schedule registration. Never delete here.
    source = Path(__file__).with_name('docker-image-retention.py')
    with tempfile.TemporaryDirectory() as temporary:
        preview_config = Path(temporary) / 'config.json'
        # A report can run before the permanent directory exists.
        report_config = dict(config)
        report_config['lock_path'] = str(Path(temporary) / 'report.lock')
        report_config['space_path'] = str(args.home)
        preview_config.write_text(json.dumps(report_config))
        result = subprocess.run([str(python), str(source), '--config', str(preview_config)],
                                text=True, capture_output=True, check=False, timeout=180)
        report = json.loads(result.stdout)
        if result.returncode or report['status'] != 'REPORT':
            raise ValueError(f'installation preview refused: {report.get("status")}')
    stable.mkdir(parents=True, exist_ok=True)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    old = [p for p in (helper, config_path, plist_path) if p.exists()]
    if old:
        backup = stable / ('backup-' + datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        backup.mkdir()
        for path in old:
            shutil.copy2(path, backup / path.name)
    native('bootout', f'{domain}/{LABEL}', check=False)
    atomic_write(helper, source.read_bytes())
    atomic_write(config_path, (json.dumps(config, indent=2, sort_keys=True) + '\n').encode())
    atomic_write(plist_path, plistlib.dumps(payload))
    native('bootstrap', domain, str(plist_path))
    status = native('print', f'{domain}/{LABEL}')
    print(json.dumps({**preview, 'status': 'INSTALLED', 'report': report,
                      'native_status': status.stdout}, indent=2, sort_keys=True))
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print(json.dumps({'status': 'REFUSE', 'reason': str(error)}))
        sys.exit(2)
