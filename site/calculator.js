/* The ROI calculator: two questions.

   What you pay now, and where it would run instead. That is the whole
   comparison — an earlier version asked which model and what kind of work so
   it could derive a token count, which nobody asked for and which cost two
   questions to get.

   Two rules this file exists to keep honest:

   1. On a machine the person already owns, the answer is zero. Not $15 — the
      subscription unlocks machines reached over SSH, and a model running on
      the machine SursumAI is installed on needs no subscription at all.
   2. A rented GPU bills by the hour whether it is answering or idle, so below
      some spend the per-token API is genuinely cheaper. The calculator says
      so. One that only ever says yes is an advert. */

/* Rental prices are the median of single-GPU, verified, on-demand offers on
   Vast.ai, read from its public API on the date below. The median, not the
   cheapest: the cheapest offer is one machine and may be gone by the time
   anyone clicks.

   A short list on purpose. Cards that are poor value at today's prices are
   left out rather than offered — an H100 is $3.03/h for 94GB while the RTX
   PRO 6000 is $1.60/h for 96GB, so listing both would only help someone
   choose the wrong one. */
const PRICES_ASOF = "13 October 2026";

const MACHINES = [
  { id: "own-gpu", name: "A GPU I already have", own: true, hourly: 0 },
  { id: "own-mac", name: "A Mac I already have", own: true, hourly: 0 },
  { id: "3090", name: "Rent an RTX 3090 · 24GB", hourly: 0.29 },
  { id: "4090", name: "Rent an RTX 4090 · 24GB", hourly: 0.67 },
  { id: "pro5000", name: "Rent an RTX PRO 5000 · 48GB", hourly: 0.74 },
  { id: "pro6000", name: "Rent an RTX PRO 6000 · 96GB", hourly: 1.6 },
  { id: "b200", name: "Rent a B200 · 180GB", hourly: 9.38 },
];

/* Pro is $15/month and buys one thing: machines reached over SSH. It belongs
   in the rental arithmetic and nowhere else. */
const PRO_MONTHLY = 15;
const DAYS = 30.4;

const state = { spend: 2000, machineId: "own-gpu", hoursPerDay: 24 };

const $ = (id) => document.getElementById(id);
const machine = () => MACHINES.find((m) => m.id === state.machineId) || MACHINES[0];
const money = (n) => "$" + Math.round(n).toLocaleString("en-US");

function render() {
  const m = machine();
  /* Hours only mean something when the meter is running. */
  $("calcHoursRow").hidden = !!m.own;
  $("calcSpendOut").textContent = money(state.spend);

  const rental = m.own ? 0 : m.hourly * state.hoursPerDay * DAYS;
  const cost = m.own ? 0 : rental + PRO_MONTHLY;
  const saving = state.spend - cost;

  if (m.own) {
    $("calcOut").innerHTML = `
      <div class="calc-headline calc-good">
        <b>$0</b><span>a month. You stop paying, entirely.</span>
      </div>
      <p class="calc-read">The model runs on hardware you already own, and
        SursumAI is free and MIT-licensed. The $15 subscription is only for
        deploying to machines you reach over SSH &mdash; this is not one of
        them.</p>`;
    return;
  }

  const hours = state.hoursPerDay === 24 ? "running all month" : `at ${state.hoursPerDay}h a day`;

  $("calcOut").innerHTML = saving > 0
    ? `<div class="calc-headline calc-good">
        <b>${money(saving)}</b>
        <span>saved a month &mdash; ${money(saving * 12)} a year</span>
      </div>
      <p class="calc-read">${money(rental)} of GPU ${hours}, plus
        ${money(PRO_MONTHLY)} for Pro, against the ${money(state.spend)} you
        pay today.</p>`
    : `<div class="calc-headline calc-bad">
        <b>Not yet</b><span>at this spend, the API is the cheaper option</span>
      </div>
      <p class="calc-read">That GPU is ${money(cost)} a month ${hours}, and you
        spend ${money(state.spend)}. It pays off above
        <strong>${money(cost)} a month</strong> &mdash; or sooner, if the work
        is a batch job and the machine need not be awake all day. A machine you
        already own costs nothing and pays off immediately.</p>`;
}

document.addEventListener("DOMContentLoaded", () => {
  if (!$("calcOut")) return;
  $("calcAsof").textContent = PRICES_ASOF;

  $("calcMachine").innerHTML = MACHINES.map(
    (m) => `<option value="${m.id}">${m.name}${m.own ? "" : ` — $${m.hourly.toFixed(2)}/h`}</option>`
  ).join("");
  $("calcMachine").value = state.machineId;
  $("calcSpend").value = state.spend;
  $("calcSpendRange").value = state.spend;

  const setSpend = (v) => {
    state.spend = Math.max(0, Math.min(200000, +v || 0));
    render();
  };
  $("calcSpend").addEventListener("input", (e) => {
    setSpend(e.target.value);
    $("calcSpendRange").value = Math.min(state.spend, 20000);
  });
  $("calcSpendRange").addEventListener("input", (e) => {
    setSpend(e.target.value);
    $("calcSpend").value = state.spend;
  });
  $("calcMachine").addEventListener("change", (e) => {
    state.machineId = e.target.value;
    render();
  });
  $("calcHours").addEventListener("change", (e) => {
    state.hoursPerDay = +e.target.value;
    render();
  });

  render();
});
