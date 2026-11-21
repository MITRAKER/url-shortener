// Load test: redirects, the way real traffic asks for them.
//
//   make loadtest                      # the Docker stack, rate limits raised
//   make loadtest RATE=500 DURATION=2m
//   k6 run -e BASE_URL=http://localhost:8000 loadtest/redirects.js
//
// An *open* model: k6 starts RATE requests every second, however slowly the
// answers come, as real users do. (A *closed* model, a fixed number of users
// each waiting for its answer before asking again, slows down when the
// server does, and so hides how slow it got: "coordinated omission".
// LEARNING.md, lesson 6.)

import http from "k6/http";
import { check, fail } from "k6";

const BASE_URL = __ENV.BASE_URL || "http://localhost:8000";
const RATE = Number(__ENV.RATE || 200); // requests started per second
const DURATION = __ENV.DURATION || "1m";
const LINKS = Number(__ENV.LINKS || 100); // links created before the test starts
// A few requests ask for codes that don't exist, as typos and scanners do:
// they get 404, and Redis remembers "not found" for a minute.
const UNKNOWN_SHARE = 0.05;

export const options = {
  // A redirect's answer is the 302 itself: don't follow it to example.com.
  maxRedirects: 0,
  scenarios: {
    redirects: {
      executor: "constant-arrival-rate",
      rate: RATE,
      timeUnit: "1s",
      duration: DURATION,
      // Virtual users (VUs) are k6's workers, each waiting for one answer at
      // a time. If they are all busy, k6 can't start a request on time, and
      // counts it in dropped_iterations: the server didn't keep up.
      preAllocatedVUs: Math.max(10, Math.ceil(RATE / 10)),
      maxVUs: Math.max(50, RATE),
    },
  },
  // Pass or fail. The objectives of monitoring/prometheus/alerts.yml, seen
  // from outside: with Nginx and the network included.
  thresholds: {
    http_req_failed: ["rate<0.001"],
    "http_req_duration{kind:redirect}": ["p(99)<100"],
    checks: ["rate>0.999"],
    dropped_iterations: ["count==0"],
  },
  summaryTrendStats: ["avg", "med", "p(90)", "p(99)", "max"],
};

// Popularity follows Zipf's law, as on real sites: the k-th most popular link
// gets clicks in proportion to 1/k. With 100 links, the top one gets 19% of
// them, and the top ten 56%. cumulative[k] adds up the weights of links 0..k.
const cumulative = [];
let total = 0;
for (let k = 1; k <= LINKS; k++) {
  total += 1 / k;
  cumulative.push(total);
}

function popularLink() {
  // The first link whose cumulative weight reaches a random point: binary search.
  const point = Math.random() * total;
  let low = 0;
  let high = cumulative.length - 1;
  while (low < high) {
    const middle = (low + high) >> 1;
    if (cumulative[middle] < point) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  return low;
}

// Runs once, before the load: create the links to click.
export function setup() {
  const codes = [];
  for (let i = 0; i < LINKS; i++) {
    const response = http.post(
      `${BASE_URL}/api/links`,
      JSON.stringify({ url: `https://example.com/page/${i}` }),
      { headers: { "Content-Type": "application/json" }, tags: { kind: "setup" } },
    );
    if (response.status !== 201) {
      fail(`creating link ${i} returned ${response.status}: ${response.body}`);
    }
    codes.push(response.json("code"));
  }
  return { codes };
}

// Runs RATE times a second. The "name" tag groups every code under one name,
// as the app's route label does: one line in the results, not one per code.
export default function (data) {
  if (Math.random() < UNKNOWN_SHARE) {
    const response = http.get(`${BASE_URL}/nosuch${Math.floor(Math.random() * 1000)}`, {
      tags: { kind: "unknown", name: "GET /{code} (no such link)" },
      responseCallback: http.expectedStatuses(404),
    });
    check(response, { "no such link: 404": (r) => r.status === 404 });
    return;
  }
  const code = data.codes[popularLink()];
  const response = http.get(`${BASE_URL}/${code}`, {
    tags: { kind: "redirect", name: "GET /{code}" },
  });
  check(response, { "redirect: 302": (r) => r.status === 302 });
}
