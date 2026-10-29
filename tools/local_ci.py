#!/usr/bin/env python3
"""Run the build steps of .github/workflows/build-ipa.yml on this Mac, in order, in a checkout.

    local_ci.py <checkout> [--from N] [--to N] [--only N] [--list]

Skipped: `uses:` steps (checkout, caches, artifacts), steps that only run host checks
(python3 tests/host/check-*.py), and the steps that only make sense on GitHub (sudo
xcode-select, the OTA bucket, the encrypted artifact, the failure dump). The native Homebrew
(/opt/homebrew) comes first on PATH, as on GitHub's arm64 runner. Everything else
runs as the job would: bash -eo pipefail, the step's env and working directory,
GITHUB_ENV / GITHUB_OUTPUT / GITHUB_PATH, `${{ }}` expressions and `if:`. Caches always
miss, so every build step runs. State (env, outputs, PATH) is saved after each step, so
--from N resumes. State and each step's output (logs/NNN-<name>.log) go to
<checkout>/toolchains/local-ci (ignored by git). docs/local-build/README.md has the whole
local build, from this to the signed app on a device.
"""
import argparse, glob, hashlib, json, os, re, subprocess, sys, time

LOGS = STATE = HERE = None   # set in main(): <checkout>/toolchains/local-ci
REPO = 'rtmtree/bcd-games-persona'
SKIP_NAMES = {
    'Select newest Xcode',
    'Encrypt the IPA to the repository owner\'s SSH keys',
    'Sign for OTA install (private bucket)',
    'Check the IPA reached the private bucket',
    'Dump first build errors (on failure)',
}


def load_steps(checkout):
    out = subprocess.run(['ruby', '-ryaml', '-rjson', '-e',
                          'puts JSON.generate(YAML.load_file(ARGV[0])["jobs"]["build"]["steps"])',
                          os.path.join(checkout, '.github/workflows/build-ipa.yml')],
                         check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def only_checks(step):
    lines = [l.strip() for l in step.get('run', '').split('\n') if l.strip() and not l.strip().startswith('#')]
    return bool(lines) and all(re.match(r'^python3 tests/host/check-[\w-]+\.py( --[\w-]+)*$', l) for l in lines)


def skipped(step):
    if 'run' not in step:
        return 'uses ' + step.get('uses', '?')
    if only_checks(step):
        return 'host check'
    if step.get('name') in SKIP_NAMES:
        return 'GitHub only'
    return None


class Ctx:
    def __init__(self, checkout, state):
        self.checkout = checkout
        self.env = state.get('env', {})
        self.outputs = state.get('outputs', {})
        self.path = state.get('path', [])

    def save(self):
        with open(STATE, 'w') as f:
            json.dump({'env': self.env, 'outputs': self.outputs, 'path': self.path}, f, indent=1)

    def value(self, expr):
        expr = expr.strip()
        m = re.fullmatch(r'steps\.([\w-]+)\.outputs\.([\w-]+)', expr)
        if m:
            return self.outputs.get(m.group(1), {}).get(m.group(2), '')
        m = re.fullmatch(r'steps\.([\w-]+)\.outcome', expr)
        if m:
            return 'success'
        m = re.fullmatch(r'env\.(\w+)', expr)
        if m:
            return self.env.get(m.group(1), os.environ.get(m.group(1), ''))
        if expr.startswith('secrets.'):
            return ''
        fixed = {'github.repository': REPO, 'github.repository_owner': REPO.split('/')[0], 'github.run_number': '1',
                 'github.workspace': self.checkout, 'runner.os': 'macOS', 'runner.arch': 'ARM64',
                 'github.ref_name': 'build/p3r', 'github.event_name': 'push'}
        if expr in fixed:
            return fixed[expr]
        m = re.fullmatch(r"hashFiles\((.*)\)", expr)
        if m:
            h = hashlib.sha256()
            for pat in re.findall(r"'([^']*)'", m.group(1)):
                for p in sorted(glob.glob(os.path.join(self.checkout, pat), recursive=True)):
                    if os.path.isfile(p):
                        h.update(open(p, 'rb').read())
            return h.hexdigest()
        if re.fullmatch(r"'[^']*'", expr):
            return expr[1:-1]
        return self.cond(expr)

    def subst(self, text):
        if not isinstance(text, str):
            return text
        return re.sub(r'\$\{\{(.*?)\}\}', lambda m: str(self.value(m.group(1))), text)

    def cond(self, expr):
        if expr is None or expr == '':
            return True
        if isinstance(expr, bool):
            return expr
        e = expr.strip()
        if e.startswith('${{') and e.endswith('}}'):
            e = e[3:-2].strip()
        e = e.replace('always()', 'True').replace('success()', 'True').replace('failure()', 'False').replace('cancelled()', 'False')
        def lit(m):
            v = self.value(m.group(0))
            return repr(v)
        e = re.sub(r"hashFiles\([^)]*\)|steps\.[\w-]+\.(?:outputs\.[\w-]+|outcome)|env\.\w+|github\.[\w.]+|runner\.\w+|secrets\.\w+", lit, e)
        e = e.replace('&&', ' and ').replace('||', ' or ').replace('!=', ' != ').replace('==', ' == ')
        e = re.sub(r'!(?!=)', ' not ', e)
        return bool(eval(e, {'__builtins__': {}}, {'True': True, 'False': False}))


def read_kv(path):
    out = {}
    if not os.path.exists(path):
        return out
    lines = open(path).read().split('\n')
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r'^([^=<]+)<<(.+)$', line)
        if m:
            key, delim, val = m.group(1), m.group(2), []
            i += 1
            while i < len(lines) and lines[i] != delim:
                val.append(lines[i]); i += 1
            out[key] = '\n'.join(val)
        elif '=' in line:
            k, v = line.split('=', 1)
            out[k] = v
        i += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('checkout')
    ap.add_argument('--from', dest='start', type=int, default=0)
    ap.add_argument('--to', type=int, default=10 ** 6)
    ap.add_argument('--only', type=int)
    ap.add_argument('--list', action='store_true')
    a = ap.parse_args()
    checkout = os.path.abspath(a.checkout)
    global HERE, LOGS, STATE
    HERE = os.path.join(checkout, 'toolchains', 'local-ci')
    LOGS, STATE = os.path.join(HERE, 'logs'), os.path.join(HERE, 'state.json')
    os.makedirs(HERE, exist_ok=True)
    steps = load_steps(checkout)
    if a.list:
        for i, s in enumerate(steps):
            why = skipped(s)
            print(f"{i:3d} {'skip ('+why+')' if why else 'run '} {s.get('name', s.get('uses', '?'))}")
        return 0
    os.makedirs(LOGS, exist_ok=True)
    state = json.load(open(STATE)) if (a.start or a.only is not None) and os.path.exists(STATE) else {}
    ctx = Ctx(checkout, state)
    tmp = os.path.join(HERE, 'runner-temp'); os.makedirs(tmp, exist_ok=True)
    lo, hi = (a.only, a.only) if a.only is not None else (a.start, a.to)
    for i, s in enumerate(steps):
        if i < lo or i > hi:
            continue
        name = s.get('name', s.get('uses', '?'))
        why = skipped(s)
        if why:
            continue
        if not ctx.cond(s.get('if')):
            print(f"[{i:3d}] skip (if: {s.get('if')}) {name}", flush=True)
            continue
        env = dict(os.environ)
        env.update(ctx.env)
        env.update({'GITHUB_WORKSPACE': checkout, 'GITHUB_ACTIONS': 'true', 'CI': 'true', 'RUNNER_OS': 'macOS',
                    'RUNNER_ARCH': 'ARM64', 'RUNNER_TEMP': tmp, 'GITHUB_REPOSITORY': REPO, 'GITHUB_RUN_NUMBER': '1',
                    'GITHUB_REPOSITORY_OWNER': REPO.split('/')[0], 'HOMEBREW_NO_AUTO_UPDATE': '1',
                    'HOMEBREW_NO_INSTALL_CLEANUP': '1', 'HOMEBREW_NO_ENV_HINTS': '1'})
        # The native Homebrew first, as on GitHub's arm64 runner (an Intel /usr/local Homebrew has no bottles any more).
        env['PATH'] = '/opt/homebrew/bin:/opt/homebrew/sbin:' + env['PATH']
        if ctx.path:
            env['PATH'] = ':'.join(reversed(ctx.path)) + ':' + env['PATH']
        for k, v in (s.get('env') or {}).items():
            env[k] = str(ctx.subst(v))
        files = {k: os.path.join(tmp, f'{k.lower()}-{i}') for k in ('GITHUB_ENV', 'GITHUB_OUTPUT', 'GITHUB_PATH', 'GITHUB_STEP_SUMMARY')}
        for k, p in files.items():
            open(p, 'w').close(); env[k] = p
        script = os.path.join(tmp, f'step-{i}.sh')
        shell = s.get('shell', 'bash')
        body = ctx.subst(s['run'])
        with open(script, 'w') as f:
            f.write(body)
        if shell == 'bash' or shell is None:
            cmd = ['bash', '--noprofile', '--norc', '-eo', 'pipefail', script]
        elif shell.startswith('python'):
            cmd = ['python3', script]
        elif shell == 'sh':
            cmd = ['sh', '-e', script]
        else:
            cmd = shell.replace('{0}', script).split()
        wd = os.path.join(checkout, ctx.subst(s.get('working-directory', '')) or '')
        log = os.path.join(LOGS, f"{i:03d}-{re.sub(r'[^A-Za-z0-9]+', '-', name)[:60].strip('-')}.log")
        t0 = time.time()
        print(f"[{i:3d}] run  {name} ...", flush=True)
        with open(log, 'w') as lf:
            rc = subprocess.run(cmd, cwd=wd, env=env, stdout=lf, stderr=subprocess.STDOUT).returncode
        dt = time.time() - t0
        ctx.env.update(read_kv(files['GITHUB_ENV']))
        ctx.outputs.setdefault(s.get('id', f'_{i}'), {}).update(read_kv(files['GITHUB_OUTPUT']))
        ctx.path += [p for p in open(files['GITHUB_PATH']).read().split('\n') if p.strip()]
        ctx.save()
        coe = s.get('continue-on-error', False)
        coe = ctx.cond(coe) if isinstance(coe, str) else bool(coe)
        status = 'ok' if rc == 0 else ('FAILED (continues)' if coe else 'FAILED')
        print(f"[{i:3d}] {status} in {dt:.0f}s  {name}  ({log})", flush=True)
        if rc != 0 and not coe:
            print(open(log).read()[-4000:])
            print(f"\nResume after fixing with: {sys.argv[0]} {a.checkout} --from {i}")
            return 1
    print('done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
