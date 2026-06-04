// Crew Management System — k6 load test.
//
// ⚠ Run against a STAGING backend (seeded with scripts/seed_load_test.py), NOT production.
//
// Usage (one scenario at a time):
//   k6 run -e BASE_URL=https://STAGING/api/v1 -e TOKEN=<jwt> -e CREW_ID=<id> \
//          -e YEAR=2025 -e MONTH=8 -e SCENARIO=load100 scripts/loadtest.k6.js
//
// SCENARIO = load50 | load100 | load200 | stress | soak   (default load50)
//
// Acceptance thresholds encoded below:
//   • normal requests  p95 < 1500ms
//   • report requests  p95 < 5000ms
//   • error rate       < 1%

import http from 'k6/http';
import { check, sleep, group } from 'k6';

const BASE = __ENV.BASE_URL;
const TOKEN = __ENV.TOKEN;
const CREW_ID = __ENV.CREW_ID || '';
const YEAR = __ENV.YEAR || '2025';
const MONTH = __ENV.MONTH || '8';
const SCEN = __ENV.SCENARIO || 'load50';

const H = { headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' } };

const SCENARIOS = {
  load50:  { executor: 'constant-vus', vus: 50,  duration: '3m' },
  load100: { executor: 'constant-vus', vus: 100, duration: '3m' },
  load200: { executor: 'constant-vus', vus: 200, duration: '3m' },
  // ramp up until things break — watch where error rate / p95 explode
  stress: {
    executor: 'ramping-vus', startVUs: 0,
    stages: [
      { duration: '2m', target: 100 },
      { duration: '2m', target: 200 },
      { duration: '2m', target: 400 },
      { duration: '2m', target: 700 },
      { duration: '2m', target: 1000 },
      { duration: '1m', target: 0 },
    ],
  },
  // sustained moderate load for 45 min — memory leaks, pool exhaustion, cold starts
  soak: { executor: 'constant-vus', vus: 60, duration: '45m' },
};

export const options = {
  scenarios: { [SCEN]: SCENARIOS[SCEN] },
  thresholds: {
    'http_req_failed': ['rate<0.01'],
    'http_req_duration{type:normal}': ['p(95)<1500', 'p(99)<2500'],
    'http_req_duration{type:report}': ['p(95)<5000'],
  },
};

function get(path, type) {
  const r = http.get(`${BASE}${path}`, Object.assign({ tags: { type } }, H));
  check(r, { [`${path} ok`]: (res) => res.status === 200 });
  return r;
}

export default function () {
  group('normal', () => {
    get('/dashboard/stats', 'normal');
    get('/crew?page=1&page_size=20', 'normal');
    get('/flights?page=1&page_size=20', 'normal');
  });

  group('report', () => {
    get(`/reports/monthly-hours/matrix?year=${YEAR}&month=${MONTH}&only_with_hours=true`, 'report');
    if (CREW_ID) {
      get(`/reports/monthly-hours/statement?crew_id=${CREW_ID}&year=${YEAR}&month=${MONTH}`, 'report');
    }
  });

  sleep(Math.random() * 2 + 1); // 1–3s think time
}

// Heavy exports are intentionally NOT hammered here — run them as a separate,
// low-rate probe (a handful of requests) and watch for Vercel function timeouts;
// if they exceed the function limit at scale, move exports to a background job
// (see tasks/backend_audit.md, "Export performance").
