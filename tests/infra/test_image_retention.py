"""Black-box retention tests with an external Docker fixture process."""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / 'scripts/docker-image-retention.py'
INSTALLER = ROOT / 'scripts/install-image-retention.py'


def iid(n: int) -> str:
    return 'sha256:' + f'{n:064x}'


FAKE = r'''#!/usr/bin/env python3
import json,os,pathlib,sys
p=pathlib.Path(os.environ['DOCKER_FIXTURE']);d=json.loads(p.read_text())
a=sys.argv[1:];assert a[:2]==['--context','fixture'],a
a=a[2:]
with p.with_suffix('.log').open('a') as log:log.write(json.dumps(a)+'\n')
if d.get('offline'):print('Cannot connect to Docker daemon',file=sys.stderr);sys.exit(1)
if a==['info','--format','{{json .}}']:print(json.dumps({'ID':d.get('engine','engine-one')}))
elif a==['image','ls','-aq','--no-trunc']:print('\n'.join(i['Id'] for i in d['images']))
elif a[:2]==['image','inspect']:print(json.dumps([i for i in d['images'] if i['Id'] in a[2:]]))
elif a==['container','ls','-aq','--no-trunc']:print('\n'.join(c['Id'] for c in d.get('containers',[])))
elif a[:2]==['container','inspect']:print(json.dumps(d.get('containers',[])))
elif a==['system','df']:print('Images fixture disk usage')
elif a[:3]==['image','rm','--no-prune']:
 assert len(a)==4 and len(a[3])==71,a
 if d.get('conflict'):print('conflict',file=sys.stderr);sys.exit(1)
 d['images']=[i for i in d['images'] if i['Id']!=a[3]]
 if d.get('protect_after_delete'):d['containers']=[{'Id':'c1','Image':d['protect_after_delete']}]
 p.write_text(json.dumps(d));print('Deleted: '+a[3])
else:raise AssertionError(a)
'''


class RetentionCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.docker = self.home / 'docker'
        self.docker.write_text(FAKE)
        self.docker.chmod(0o755)
        self.probe = self.home / 'ps'
        self.probe.write_text('#!/bin/sh\nexit 0\n')
        self.probe.chmod(0o755)
        self.fixture = self.home / 'fixture.json'
        self.config_path = self.home / 'config.json'
        self.data: dict = {'images': []}
        self.config: dict = {
            'docker': str(self.docker), 'context': 'fixture', 'engine_id': 'engine-one',
            'groups': {'msai': ['msai-backend', 'msai-frontend']}, 'protected_refs': [],
            'lock_path': str(self.home / 'lock'), 'space_path': str(self.home),
            'min_free_bytes': 0, 'mode': 'local', 'process_probe': str(self.probe),
        }

    def image(self, n: int, tag: str | None = None) -> None:
        self.data['images'].append({'Id': iid(n), 'RepoTags': [tag or f'msai-backend:r{n}'],
                                   'RepoDigests': [], 'Created': f'2020-01-{n:02}T00:00:00Z'})

    def run_cli(self, *args: str, expected: int = 0) -> dict:
        self.fixture.write_text(json.dumps(self.data))
        self.config_path.write_text(json.dumps(self.config))
        result = subprocess.run([sys.executable, str(CLI), '--config', str(self.config_path), *args],
                                env={**os.environ, 'DOCKER_FIXTURE': str(self.fixture)},
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def removals(self) -> list:
        log = self.fixture.with_suffix('.log')
        return [json.loads(x) for x in log.read_text().splitlines()
                if json.loads(x)[:2] == ['image', 'rm']] if log.exists() else []

    def test_report_default_group_reserve(self) -> None:
        self.config['keep_recent'] = 3
        for n in range(1, 6):
            self.image(n, f'msai-{"frontend" if n % 2 else "backend"}:r{n}')
        self.assertEqual(self.run_cli()['candidates'], [iid(1), iid(2)])
        self.assertEqual(self.removals(), [])

    def test_protections_and_unknown_created(self) -> None:
        for n in range(1, 16):
            self.image(n)
        self.data['containers'] = [{'Id': 'running', 'Image': iid(1)}, {'Id': 'stopped', 'Image': iid(2)}]
        self.config['protected_refs'] = ['msai-backend:r3', iid(4)]
        self.data['images'][4]['RepoTags'].append('unrelated:alias')
        self.data['images'][5]['RepoTags'].append('msai-frontend:alias')
        self.data['images'][6]['RepoTags'] = []
        self.data['images'][7]['Created'] = ''
        self.data['images'][8]['RepoTags'] = ['msai-surprise:r9']
        self.data['images'][9]['Created'] = '2099-01-01T00:00:00Z'
        self.data['images'][10]['Created'] = 'invalid'
        report = self.run_cli()
        self.assertEqual(report['candidates'], [iid(12)])
        reasons = {x['id']: x['reasons'] for x in report['images']}
        self.assertIn('unknown-created', reasons[iid(8)])
        self.assertEqual(report['unapproved_repositories'], ['msai-surprise'])

    def test_creation_grace(self) -> None:
        from datetime import datetime, timedelta, timezone
        for n in range(1, 6):
            self.image(n)
        now = datetime.now(timezone.utc)
        for n in range(2, 6):
            self.data['images'][n - 1]['Created'] = (now - timedelta(days=6 - n)).isoformat()
        report = self.run_cli()
        self.assertEqual(report['candidates'], [iid(1)])
        reasons = {record['id']: record['reasons'] for record in report['images']}
        self.assertIn('creation-grace', reasons[iid(2)])
        self.assertNotIn('recent-reserve', reasons[iid(2)])
        self.assertEqual(reasons[iid(1)], [])

    def test_apply_explicit_full_id(self) -> None:
        for n in range(1, 6):
            self.image(n)
        self.run_cli('--apply', expected=2)
        self.assertEqual(self.removals(), [])
        self.assertEqual(self.run_cli('--apply', '--approve-id', iid(1))['removed'], [iid(1)])
        self.assertEqual(self.removals(), [['image', 'rm', '--no-prune', iid(1)]])

    def test_fresh_protections_and_native_conflict(self) -> None:
        for n in range(1, 6):
            self.image(n)
        self.data['protect_after_delete'] = iid(2)
        self.assertEqual(self.run_cli('--apply', '--approve-policy')['removed'], [iid(1)])
        self.assertEqual(len(self.removals()), 1)
        self.data['conflict'] = True
        self.run_cli('--apply', '--approve-id', iid(1), expected=2)

    def test_engine_inventory_and_overlap(self) -> None:
        self.image(1)
        self.data['engine'] = 'wrong'
        self.run_cli('--apply', '--approve-policy', expected=2)
        self.data['engine'] = 'engine-one'
        self.data['images'][0]['Id'] = 'short'
        self.run_cli('--apply', '--approve-policy', expected=2)
        with (self.home / 'lock').open('w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.assertEqual(self.run_cli()['status'], 'SKIP_OVERLAP')
        self.assertEqual(self.removals(), [])

    def test_observed_build_skips_all_mutations(self) -> None:
        for n in range(1, 6):
            self.image(n)
        self.probe.write_text('#!/bin/sh\necho "123 /usr/local/bin/docker buildx build -t msai-backend:new ."\n')
        self.assertEqual(self.run_cli('--apply', '--approve-policy')['status'], 'SKIP_BUILD_OR_DEPLOY')
        self.assertEqual(self.removals(), [])

    def test_ambiguous_repository_membership_is_preserved(self) -> None:
        for n in range(1, 6):
            self.image(n)
        self.config['groups']['overlap'] = ['msai-backend']
        self.assertEqual(self.run_cli()['candidates'], [])

    def test_installer_fixture_registration_status_and_operator_pin_update(self) -> None:
        installer = self.home / 'install-image-retention.py'
        source = INSTALLER.read_text()
        launchctl = self.home / 'launchctl'
        launchctl.write_text('''#!/usr/bin/env python3
import pathlib,sys
p=pathlib.Path(__file__).with_suffix('.state')
if sys.argv[1]=='bootstrap':p.write_text(sys.argv[-1])
elif sys.argv[1]=='bootout':p.unlink(missing_ok=True)
elif sys.argv[1]=='print':
 if not p.exists():sys.exit(1)
 print('fixture registered '+p.read_text())
else:sys.exit(2)
''')
        launchctl.chmod(0o755)
        # The adapter confines native launchd effects to an external executable fixture.
        installer.write_text(source.replace("'/bin/launchctl'", repr(str(launchctl)))
                             .replace("platform.system() != 'Darwin'", 'False'))
        (self.home / CLI.name).write_bytes(CLI.read_bytes())
        self.fixture.write_text(json.dumps(self.data))
        self.config['protected_refs'] = ['operator:original']
        self.config_path.write_text(json.dumps(self.config))
        command = [sys.executable, str(installer), '--home', str(self.home), '--config',
                   str(self.config_path), '--python', sys.executable]
        env = {**os.environ, 'DOCKER_FIXTURE': str(self.fixture)}
        first = subprocess.run([*command, '--apply'], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
        payload = json.loads(first.stdout)
        self.assertEqual(payload['status'], 'INSTALLED')
        stable_config = Path(payload['config_path'])
        installed = json.loads(stable_config.read_text())
        installed['protected_refs'].append('operator:installed')
        stable_config.write_text(json.dumps(installed))
        second = subprocess.run([*command, '--apply'], env=env, text=True, capture_output=True, check=False)
        self.assertEqual(second.returncode, 0, second.stdout + second.stderr)
        self.assertIn('operator:installed', json.loads(stable_config.read_text())['protected_refs'])
        self.assertTrue(list(stable_config.parent.glob('backup-*')))
        status = subprocess.run([sys.executable, str(installer), '--home', str(self.home), '--status'],
                                env=env, text=True, capture_output=True, check=False)
        self.assertEqual(status.returncode, 0, status.stdout + status.stderr)
        self.assertEqual(json.loads(status.stdout)['status'], 'LOADED')
        self.assertEqual(self.removals(), [])

    def test_offline_and_disk_pressure(self) -> None:
        self.data['offline'] = True
        self.assertEqual(self.run_cli()['status'], 'SKIP_DOCKER_UNAVAILABLE')
        self.data['offline'] = False
        self.config['min_free_bytes'] = 2**63
        self.assertEqual(self.run_cli(expected=3)['status'], 'DISK_PRESSURE')

    def test_deployment_authority_fallback_sentinel_never_eval(self) -> None:
        for n in range(1, 6):
            self.image(n)
        current = self.home / 'current.env'
        good = self.home / 'good.env'
        current.write_text('MSAI_GIT_SHA=0000001\nMSAI_REGISTRY=registry.test\nMSAI_BACKEND_IMAGE=backend\nMSAI_FRONTEND_IMAGE=frontend\n')
        good.write_text('MSAI_FIRST_DEPLOY=1\n')
        self.config['deployment_env'] = [
            {'name': 'current', 'paths': [str(self.home / 'missing'), str(current)]},
            {'name': 'last-good', 'paths': [str(good)]}]
        self.config['mode'] = 'vm'
        self.run_cli('--quiet-deployment')
        self.run_cli('--apply', '--approve-policy', expected=2)
        current.write_text('MSAI_GIT_SHA=$(touch '+str(self.home / 'bad')+')\n')
        self.run_cli('--quiet-deployment', '--apply', '--approve-policy', expected=2)
        self.assertFalse((self.home / 'bad').exists())
        current.unlink()
        self.run_cli('--quiet-deployment', expected=2)

    def test_current_and_last_good_authorities_pin_distinct_old_images(self) -> None:
        tags = [
            'registry.test/backend:0000001', 'registry.test/frontend:0000001',
            'registry.test/backend:0000002', 'registry.test/frontend:0000002',
            'registry.test/backend:0000005', 'registry.test/backend:0000006',
            'registry.test/frontend:0000007', 'registry.test/backend:0000008',
        ]
        for n, tag in enumerate(tags, 1):
            self.image(n, tag)
        self.config['mode'] = 'vm'
        self.config['groups'] = {'vm': ['registry.test/backend', 'registry.test/frontend']}
        self.config['deployment_env'] = []
        for name, sha in (('current', '0000001'), ('last-good', '0000002')):
            authority = self.home / f'{name}.env'
            authority.write_text(f'MSAI_GIT_SHA={sha}\nMSAI_REGISTRY=registry.test\n'
                                 'MSAI_BACKEND_IMAGE=backend\nMSAI_FRONTEND_IMAGE=frontend\n')
            self.config['deployment_env'].append({'name': name, 'paths': [str(authority)]})
        report = self.run_cli('--quiet-deployment')
        self.assertCountEqual(report['pins'], tags[:4])
        self.assertEqual(report['candidates'], [iid(5)])
        reasons = {record['id']: record['reasons'] for record in report['images']}
        for n in range(1, 5):
            self.assertIn('protected-pin', reasons[iid(n)])
            self.assertNotIn('recent-reserve', reasons[iid(n)])
        self.assertEqual(reasons[iid(5)], [])

    def test_zero_reserve_preserves_release_container_and_prospective_pins(self) -> None:
        tags = [
            'registry.test/backend:0000001', 'registry.test/backend:0000002',
            'registry.test/backend:65ae682', 'registry.test/backend:71aa4a9',
            'registry.test/backend:23db3b8', 'registry.test/backend:0000006',
            'registry.test/frontend:0000007', 'postgres:16-alpine',
        ]
        for n, tag in enumerate(tags, 1):
            self.image(n, tag)
        self.config.update({'mode': 'vm', 'keep_recent': 0,
                            'groups': {'vm': ['registry.test/backend', 'registry.test/frontend']},
                            'deployment_env': []})
        for name, sha in (('current', '0000001'), ('last-good', '0000002')):
            authority = self.home / f'{name}.env'
            authority.write_text(f'MSAI_GIT_SHA={sha}\nMSAI_REGISTRY=registry.test\n'
                                 'MSAI_BACKEND_IMAGE=backend\nMSAI_FRONTEND_IMAGE=frontend\n')
            self.config['deployment_env'].append({'name': name, 'paths': [str(authority)]})
        self.data['containers'] = [{'Id': 'old-supervisor', 'Image': iid(3)}]
        report = self.run_cli('--quiet-deployment', '--pin', tags[5])
        self.assertCountEqual(report['pins'], [
            tags[0], tags[1], 'registry.test/frontend:0000001',
            'registry.test/frontend:0000002', tags[5],
        ])
        eligible = [iid(4), iid(5), iid(7)]
        self.assertEqual(report['candidates'], eligible)
        reasons = {record['id']: record['reasons'] for record in report['images']}
        for n in (1, 2, 6):
            self.assertIn('protected-pin', reasons[iid(n)])
        self.assertIn('container-reference', reasons[iid(3)])
        self.assertIn('unknown-or-mixed-ownership', reasons[iid(8)])
        self.assertTrue(all('recent-reserve' not in value for value in reasons.values()))
        applied = self.run_cli('--quiet-deployment', '--pin', tags[5], '--apply', '--approve-policy')
        self.assertEqual(applied['removed'], eligible)
        self.assertEqual(self.removals(), [['image', 'rm', '--no-prune', value] for value in eligible])

    def test_invalid_reserve_values_still_refuse(self) -> None:
        self.image(1)
        for value in (-1, 0.5, '0', True):
            with self.subTest(keep_recent=value):
                self.config['keep_recent'] = value
                self.assertEqual(self.run_cli('--apply', '--approve-policy', expected=2)['status'], 'REFUSE')
        self.assertEqual(self.removals(), [])


class Integration(unittest.TestCase):
    def test_installer_default_lean_configs_preserve_local_qualification_pins(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            previews = {}
            for target in ('mac', 'vm'):
                result = subprocess.run([sys.executable, str(INSTALLER), '--home', folder,
                                         '--target', target, '--python', sys.executable],
                                        text=True, capture_output=True, check=False)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                previews[target] = json.loads(result.stdout)['config']
                self.assertEqual(previews[target]['keep_recent'], 0)
                self.assertEqual(previews[target]['grace_days'], 7)
            self.assertEqual(previews['vm']['protected_refs'], [])
            self.assertCountEqual([authority['name'] for authority in previews['vm']['deployment_env']],
                                  ['current', 'last-good'])
            self.assertCountEqual(previews['mac']['protected_refs'], [
                'msai-v2-research-baseline:9d51a63', 'msai-v2-research-candidate:rc6-wip',
                'msai-t4-genuine-20261008:r7', 'msai-t5-selection-01a11f56:r18',
                'msai-t10b-01a12c43:runtime', 'msai-t10b-01a12c43:controls',
                'msai-t10b-01a12ca9:qualified-base', 'msai-t10b-01a12ca9:controls',
                'msai-t10b-01a12ca9:report-projection',
            ])

    def test_executable_deploy_floor_refusal_and_reboot_current(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            source = (ROOT / 'scripts/deploy-on-vm.sh').read_text()
            source = source[:source.index('# ─── Phase 5:')]
            source = source.replace('/opt/msai', str(home / 'opt'))
            source = source.replace('/run/', str(home / 'run') + '/')
            source = source.replace('/usr/local/lib/msai', str(home / 'lib'))
            source = source.replace('/etc/msai', str(home / 'etc'))
            source = source.replace('/usr/bin/python3', sys.executable)
            for name in ('opt', 'run', 'lib', 'etc', 'bin'):
                (home / name).mkdir()
            helper = home / 'lib/docker-image-retention.py'
            helper.write_text('import os,sys\nassert "--pin" in sys.argv\nsys.exit(int(os.environ.get("FLOOR_RC", "0")))\n')
            (home / 'etc/image-retention.json').write_text('{}')
            (home / 'bin/az').write_text('#!/bin/sh\nexit 0\n')
            (home / 'bin/sudo').write_text('#!/bin/sh\nexit 1\n')
            for tool in (home / 'bin').iterdir():
                tool.chmod(0o755)
            envfile = home / 'deploy.env'
            envfile.write_text('\n'.join(f'{k}={v}' for k, v in {
                'MSAI_ACR_NAME': 'registry', 'MSAI_ACR_LOGIN_SERVER': 'registry.test',
                'MSAI_REGISTRY': 'registry.test', 'MSAI_BACKEND_IMAGE': 'backend',
                'MSAI_FRONTEND_IMAGE': 'frontend', 'MSAI_HOSTNAME': 'example.test',
                'KV_NAME': 'kv', 'RESOURCE_GROUP': 'rg', 'DEPLOYMENT_NAME': 'fixture',
            }.items()))
            persistent = home / 'opt/msai-images.env'
            previous = 'MSAI_GIT_SHA=aaaaaaa\nMSAI_REGISTRY=registry.test\nMSAI_BACKEND_IMAGE=backend\nMSAI_FRONTEND_IMAGE=frontend\n'
            persistent.write_text(previous)
            script = home / 'deploy.sh'
            script.write_text(source)
            env = {**os.environ, 'PATH': str(home / 'bin') + ':' + os.environ['PATH'], 'FLOOR_RC': '3'}
            result = subprocess.run(['bash', str(script), 'bbbbbbb', str(envfile)],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('FAIL_IMAGE_RETENTION', result.stderr)
            self.assertEqual(persistent.read_text(), previous)
            self.assertFalse((home / 'run/msai-images.env').exists())
            self.assertFalse((home / 'opt/msai-images.last-good.env').exists())
            env['FLOOR_RC'] = '0'
            result = subprocess.run(['bash', str(script), 'bbbbbbb', str(envfile)],
                                    env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual((home / 'opt/msai-images.last-good.env').read_text(), previous)
            self.assertIn('MSAI_GIT_SHA=bbbbbbb', persistent.read_text())
            self.assertEqual(persistent.read_text(), (home / 'run/msai-images.env').read_text())

    def test_executable_rollback_restores_persistent_current(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            source = (ROOT / 'scripts/deploy-on-vm.sh').read_text()
            function = source[source.index('\nrollback() {'):source.index('trap rollback EXIT')]
            current = home / 'current.env'
            good = home / 'good.env'
            mirror = home / 'mirror.env'
            good.write_text('MSAI_GIT_SHA=aaaaaaa\n')
            current.write_text('MSAI_GIT_SHA=bbbbbbb\n')
            mirror.write_text(current.read_text())
            # Execute the real rollback function with external effects confined to a shell fixture.
            script = home / 'rollback.sh'
            script.write_text('set -eu\n' +
                f'IMAGES_ENV="{current}"\nIMAGES_LAST_GOOD="{good}"\nIMAGES_ENV_PERSIST="{mirror}"\n' +
                'rollback_required=1\nCOMPOSE_FLAGS=(fixture)\nDEFAULT_PROFILE_SERVICES=(backend)\n' +
                'docker() { return 0; }\n' + function + '\nrollback\n')
            result = subprocess.run(['bash', str(script)], text=True, capture_output=True, check=False,
                                    env={k: v for k, v in os.environ.items() if k != 'DEPLOY_START_TS'})
            self.assertEqual(result.returncode, 1)
            self.assertIn('FAIL_ROLLBACK_OK', result.stderr)
            self.assertEqual(current.read_text(), good.read_text())
            self.assertEqual(mirror.read_text(), good.read_text())

    def test_guard_order_and_rollback_persistence(self) -> None:
        source = (ROOT / 'scripts/deploy-on-vm.sh').read_text()
        self.assertLess(source.index('--quiet-deployment'), source.index('# ─── Phase 4:'))
        self.assertIn('FAIL_IMAGE_RETENTION', source)
        self.assertIn('IMAGES_LAST_GOOD_PERSIST=', source)
        self.assertIn('cp -p "$IMAGES_ENV_PERSIST" "$IMAGES_ENV"', source)
        rollback = source[source.index('\nrollback() {'):source.index('trap rollback EXIT')]
        self.assertIn('"${IMAGES_ENV_PERSIST}.tmp"', rollback)

    def test_old_sha_staging_and_ci(self) -> None:
        source = (ROOT / '.github/workflows/deploy.yml').read_text()
        self.assertIn('if [[ -f scripts/docker-image-retention.py ]]; then', source)
        self.assertIn("-p 'test_image_retention.py'", (ROOT / '.github/workflows/ci.yml').read_text())

    def test_installer_preview_stable_paths_and_pins(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            home = Path(folder)
            config = home / 'input.json'
            config.write_text(json.dumps({'protected_refs': ['operator:keep']}))
            result = subprocess.run([sys.executable, str(INSTALLER), '--home', str(home),
                                     '--config', str(config), '--python', sys.executable],
                                    text=True, capture_output=True, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            preview = json.loads(result.stdout)
            self.assertFalse((home / 'Library/LaunchAgents').exists())
            self.assertIn('operator:keep', preview['config']['protected_refs'])
            self.assertIn('msai-v2-research-baseline:9d51a63', preview['config']['protected_refs'])
            plist = plistlib.loads(preview['plist'].encode())
            self.assertFalse(any('.worktrees' in p for p in plist['ProgramArguments']))
            self.assertEqual(plist['StartCalendarInterval'], {'Hour': 3, 'Minute': 15})
            self.assertIn('launchctl', INSTALLER.read_text())


if __name__ == '__main__':
    unittest.main()
