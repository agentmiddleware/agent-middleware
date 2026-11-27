// Run: node --test scripts/ci/engine-review-pass.test.mjs
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

// Execute the workflow's actual script so the gate stays inline and tests cannot
// drift onto a separate implementation. No GitHub calls leave this harness.
const workflow = readFileSync(new URL('../../.github/workflows/engine-review-pass.yml', import.meta.url), 'utf8');
const job = workflow.split(/^  engine-review-pass:\s*$/m)[1]?.split(/^  [\w-]+:/m)[0];
const script = job?.match(/^          script: \|\n((?: {12}[^\n]*\n|\n)+)/m)?.[1]
  .replace(/^ {12}/gm, '');
assert.ok(script, 'Engine Review Pass inline script must be present');
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
const runGate = new AsyncFunction('github', 'context', 'core', script);

const HEAD = 'abcdef01'.repeat(5);
const NO_PASS = 'Engine Review Pass failed: No passing review from a non-author engine for current head SHA. A marker counts only in a pull request review, not in a comment.';
const INVALID_AUTHOR = 'Invalid author engine. Please use an engine branch prefix such as gemini/ or claude/.';
const blockedBy = (engine) => `Engine Review Pass blocked: Blocked by ${engine}`;
const changesRequestedBy = (login) => `Engine Review Pass blocked: Changes requested by ${login}`;

function review(engine = 'deepseek', overrides = {}) {
  return {
    id: 1,
    state: 'APPROVED',
    submitted_at: '2026-11-18T12:00:00Z',
    author_association: 'OWNER',
    user: { login: 'trusted-reviewer' },
    body: `REVIEW: PASS ${engine} ${HEAD}`,
    ...overrides,
  };
}

async function evaluate({
  branch = 'cursor/fix',
  reviews = [],
  comments = [],
  createCommitStatus,
  allowWarnings = false,
  allowErrors = false,
  expectStatuses = 1,
} = {}) {
  const statuses = [];
  const failures = [];
  const calls = [];
  const warnings = [];
  const errors = [];
  const pr = { number: 1, head: { ref: branch, sha: HEAD } };
  const context = {
    repo: { owner: 'example', repo: 'example' },
    payload: { pull_request: pr },
    runId: 1,
    workflow: 'PR Guards',
  };
  const github = {
    rest: {
      repos: {
        createCommitStatus: createCommitStatus
          ?? (async (status) => { statuses.push(status); }),
      },
      pulls: {
        get: async () => ({ data: pr }),
        listReviews: 'reviews',
        listReviewComments: 'reviewComments',
      },
      issues: { listComments: 'issueComments' },
      actions: { listWorkflowRunsForRepo: 'runs' },
    },
    paginate: async (endpoint) => {
      calls.push(endpoint);
      if (endpoint === 'reviews') return reviews;
      if (endpoint === 'issueComments' || endpoint === 'reviewComments') return comments;
      assert.equal(endpoint, 'runs', 'Unexpected GitHub endpoint');
      return [];
    },
  };
  const core = {
    info: () => {},
    warning: (message) => warnings.push(message),
    error: (message) => errors.push(message),
    setFailed: (message) => failures.push(message),
  };
  await runGate(github, context, core);
  if (!allowErrors) assert.deepEqual(errors, [], 'The script must reach a gate verdict, not throw');
  if (!allowWarnings) assert.deepEqual(warnings, [], 'All expected GitHub calls must be mocked');
  assert.equal(statuses.length, expectStatuses);
  if (expectStatuses >= 1) {
    assert.equal(statuses[0].sha, HEAD);
    assert.equal(statuses[0].context, 'Engine Review Pass');
  }
  return { status: statuses[0], statuses, failures, calls, warnings, errors };
}

function expectVerdict(result, failure = null) {
  assert.equal(result.status.state, failure ? 'failure' : 'success');
  assert.deepEqual(result.failures, failure ? [failure] : []);
}

const reviewerEngines = [
  'claude', 'codex', 'cursor', 'gemini', 'grok', 'muse', 'kimi', 'pi',
  'deepseek', 'hermes', 'antigravity', 'visual-designer', 'product-designer',
];
const branchPrefixes = [...reviewerEngines, 'ci', 'dependabot'];

for (const engine of branchPrefixes) {
  test(`${engine}/ accepts a current-head PASS from another allowed engine`, async () => {
    const reviewer = engine === 'deepseek' ? 'codex' : 'deepseek';
    expectVerdict(await evaluate({ branch: `${engine}/fix`, reviews: [review(reviewer)] }));
  });
}

for (const engine of reviewerEngines) {
  test(`${engine}/ rejects its own engine's PASS`, async () => {
    expectVerdict(await evaluate({ branch: `${engine}/fix`, reviews: [review(engine)] }), NO_PASS);
  });
}

for (const association of ['OWNER', 'MEMBER', 'COLLABORATOR']) {
  test(`cursor/ accepts a non-author PASS from ${association}`, async () => {
    expectVerdict(await evaluate({ reviews: [review('kimi', { author_association: association })] }));
  });
}

test('branch and reviewer engines are case-insensitive', async () => {
  expectVerdict(await evaluate({ branch: 'CuRsOr/fix', reviews: [review('CoDeX')] }));
  expectVerdict(await evaluate({ branch: 'CuRsOr/fix', reviews: [review('CURSOR')] }), NO_PASS);
});

test('cursor is also an allowed non-author reviewer', async () => {
  expectVerdict(await evaluate({ branch: 'codex/fix', reviews: [review('cursor')] }));
});

for (const branch of ['unknown/fix', 'cursor', '/cursor', '']) {
  test(`invalid author branch ${JSON.stringify(branch)} fails before reading reviews`, async () => {
    const result = await evaluate({ branch, reviews: [review()] });
    expectVerdict(result, INVALID_AUTHOR);
    assert.deepEqual(result.calls, []);
  });
}

for (const sha of ['0'.repeat(40), HEAD.slice(0, 39), `${HEAD}0`, `g${HEAD.slice(1)}`]) {
  test(`cursor/ rejects stale or malformed SHA ${sha}`, async () => {
    expectVerdict(await evaluate({ reviews: [review('deepseek', { body: `REVIEW: PASS deepseek ${sha}` })] }), NO_PASS);
  });
}

for (const association of ['CONTRIBUTOR', 'NONE', '']) {
  test(`cursor/ rejects a PASS from untrusted association ${JSON.stringify(association)}`, async () => {
    expectVerdict(await evaluate({ reviews: [review('deepseek', { author_association: association })] }), NO_PASS);
  });
}

test('unknown reviewer engine cannot supply a PASS', async () => {
  expectVerdict(await evaluate({ reviews: [review('unknown')] }), NO_PASS);
});

for (const name of ['ci', 'dependabot']) {
  test(`${name}/ treats a ${name} marker as an invalid reviewer engine`, async () => {
    expectVerdict(await evaluate({ branch: `${name}/fix`, reviews: [review(name)] }), NO_PASS);
  });

  test(`cursor/ does not accept a PASS from ${name}`, async () => {
    expectVerdict(await evaluate({ reviews: [review(name)] }), NO_PASS);
  });
}

test('ci and dependabot markers cannot block a valid non-author PASS', async () => {
  expectVerdict(await evaluate({
    reviews: [
      review('ci', {
        id: 1,
        submitted_at: '2026-11-18T12:00:00Z',
        body: `REVIEW: BLOCKED ci ${HEAD}`,
      }),
      review('dependabot', {
        id: 2,
        submitted_at: '2026-11-18T12:30:00Z',
        body: `REVIEW: FAIL dependabot ${HEAD}`,
      }),
      review('deepseek', {
        id: 3,
        submitted_at: '2026-11-18T13:00:00Z',
      }),
    ],
  }));
});

test('dismissed and unsubmitted reviews cannot supply a PASS', async () => {
  expectVerdict(await evaluate({ reviews: [review('deepseek', { state: 'DISMISSED' })] }), NO_PASS);
  expectVerdict(await evaluate({ reviews: [review('deepseek', { submitted_at: null })] }), NO_PASS);
});

test('PASS markers in comments are not read or counted', async () => {
  const result = await evaluate({ comments: [review()] });
  expectVerdict(result, NO_PASS);
  assert.deepEqual(result.calls, ['reviews']);
});

// Per-engine latest marker on the current head wins; earlier markers do not stick.
test('later marker on current head supersedes an earlier one from the same engine', async () => {
  expectVerdict(await evaluate({
    reviews: [
      review('deepseek', {
        id: 1,
        submitted_at: '2026-11-18T12:00:00Z',
        body: `REVIEW: BLOCKED deepseek ${HEAD}`,
      }),
      review('deepseek', {
        id: 2,
        submitted_at: '2026-11-18T13:00:00Z',
        body: `REVIEW: PASS deepseek ${HEAD}`,
      }),
    ],
  }));
  expectVerdict(await evaluate({
    reviews: [
      review('deepseek', {
        id: 1,
        submitted_at: '2026-11-18T12:00:00Z',
        body: `REVIEW: PASS deepseek ${HEAD}`,
      }),
      review('deepseek', {
        id: 2,
        submitted_at: '2026-11-18T13:00:00Z',
        body: `REVIEW: BLOCKED deepseek ${HEAD}`,
      }),
    ],
  }), blockedBy('deepseek'));
  expectVerdict(await evaluate({
    reviews: [
      review('deepseek', {
        id: 1,
        submitted_at: '2026-11-18T12:00:00Z',
        body: `REVIEW: PASS deepseek ${HEAD}`,
      }),
      review('deepseek', {
        id: 2,
        submitted_at: '2026-11-18T13:00:00Z',
        body: `REVIEW: FAIL deepseek ${HEAD}`,
      }),
    ],
  }), blockedBy('deepseek'));
});

test('native CHANGES_REQUESTED blocks even when a valid PASS marker is present', async () => {
  expectVerdict(
    await evaluate({ reviews: [review('deepseek', { state: 'CHANGES_REQUESTED' })] }),
    changesRequestedBy('trusted-reviewer'),
  );
});

test('a later APPROVED from the same user clears native CHANGES_REQUESTED', async () => {
  expectVerdict(await evaluate({
    reviews: [
      review('deepseek', {
        id: 1,
        state: 'CHANGES_REQUESTED',
        submitted_at: '2026-11-18T12:00:00Z',
        body: '',
      }),
      review('deepseek', {
        id: 2,
        state: 'APPROVED',
        submitted_at: '2026-11-18T13:00:00Z',
      }),
    ],
  }));
});

test('untrusted CHANGES_REQUESTED does not block a valid PASS', async () => {
  expectVerdict(await evaluate({
    reviews: [
      review('deepseek', { id: 1 }),
      review('deepseek', {
        id: 2,
        state: 'CHANGES_REQUESTED',
        author_association: 'CONTRIBUTOR',
        user: { login: 'outsider' },
        body: '',
      }),
    ],
  }));
});

test('a failed success status publication fails the job', async () => {
  const result = await evaluate({
    reviews: [review()],
    createCommitStatus: async () => {
      throw new Error('status API down');
    },
    allowWarnings: true,
    allowErrors: true,
    expectStatuses: 0,
  });
  assert.equal(result.status, undefined);
  assert.deepEqual(result.failures, ['status API down']);
  assert.ok(result.warnings.some((message) => message.includes('Failed to create commit status') && message.includes('status API down')));
  assert.deepEqual(result.errors, ['Unexpected error: status API down']);
});

test('concurrency group includes the pull request head SHA', () => {
  assert.match(
    workflow,
    /group: \$\{\{ github\.workflow \}\}-engine-review-pass-\$\{\{ github\.event\.pull_request\.number \}\}-\$\{\{ github\.event\.pull_request\.head\.sha \}\}/,
  );
});
