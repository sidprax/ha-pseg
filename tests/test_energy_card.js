"use strict";

// Renders the shipped card with synthetic Home Assistant states; no browser
// or network needed. Run with `node --test tests/` (tests/test_energy_card.py
// runs it as part of pytest).
const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const SOURCE = fs.readFileSync(
  path.join(__dirname, "..", "custom_components", "pseg", "frontend", "pseg-energy-card.js"),
  "utf8"
);

// One browser: all cards share window, customElements and localStorage.
function browser() {
  const storage = new Map();
  let Card;
  class Element {
    attachShadow() {
      this.shadowRoot = { innerHTML: "", getElementById: () => null };
    }
    dispatchEvent() {}
  }
  vm.runInNewContext(SOURCE, {
    HTMLElement: Element,
    CustomEvent: class {},
    customElements: { get: () => null, define: (_name, type) => { Card = type; } },
    window: { customCards: [], location: {} },
    localStorage: {
      getItem: (key) => (storage.has(key) ? storage.get(key) : null),
      setItem: (key, value) => storage.set(key, String(value)),
      removeItem: (key) => storage.delete(key),
    },
    console: { error: (...args) => assert.fail(`Unexpected render error: ${args}`) },
  });
  return { Card, storage };
}

function reading(meter) {
  const date = new Date(Date.now() - 86400000).toISOString().slice(0, 10);
  return {
    state: "10",
    attributes: {
      meter: `Meter #${meter}`,
      latest_date: date,
      latest_kwh: 10,
      latest_cost: 2.5,
      history_14d: [{ date, kwh: 10, cost: 2.5 }],
    },
  };
}

function render(Card, entity, hass) {
  const card = new Card();
  card.setConfig({ entity });
  card.hass = hass;
  return card.shadowRoot.innerHTML;
}

test("an empty consumption response is not displayed as zero or connecting", () => {
  const { Card } = browser();
  const hass = { states: {
    "sensor.pseg_electricity_consumption": {state: "unknown", attributes: {
      consumption_data_available: false, history_14d: [], auth_required: false,
    }},
  }};
  const html = render(Card, "sensor.pseg_electricity_consumption", hass);
  assert.match(html, /No hourly consumption data received/);
  assert.doesNotMatch(html, /Connecting|0\.00/);
});

// Two accounts, as HA names them: the second account's IDs get a "_2" suffix.
function twoAccounts({ firstStatus = "connected", secondStatus = "connected" } = {}) {
  return {
    states: {
      "sensor.pseg_electricity_consumption": reading("11111111"),
      "sensor.pseg_connection_status": { state: firstStatus, attributes: {} },
      "sensor.pseg_electricity_consumption_2": reading("22222222"),
      "sensor.pseg_connection_status_2": { state: secondStatus, attributes: {} },
    },
    entities: {
      "sensor.pseg_electricity_consumption": { entity_id: "sensor.pseg_electricity_consumption", device_id: "first" },
      "sensor.pseg_connection_status": { entity_id: "sensor.pseg_connection_status", device_id: "first" },
      "sensor.pseg_electricity_consumption_2": { entity_id: "sensor.pseg_electricity_consumption_2", device_id: "second" },
      "sensor.pseg_connection_status_2": { entity_id: "sensor.pseg_connection_status_2", device_id: "second" },
    },
  };
}

const EXPIRED = "Re-authentication required";

test("a second account's card shows that account's own sign-in status", () => {
  const { Card } = browser();
  const hass = twoAccounts({ firstStatus: "connected", secondStatus: "auth_required" });

  assert.match(render(Card, "sensor.pseg_electricity_consumption_2", hass), new RegExp(EXPIRED));
  assert.doesNotMatch(render(Card, "sensor.pseg_electricity_consumption", hass), new RegExp(EXPIRED));
});

test("the first account's expired session doesn't show on the second account's card", () => {
  const { Card } = browser();
  const hass = twoAccounts({ firstStatus: "auth_required", secondStatus: "connected" });

  assert.doesNotMatch(render(Card, "sensor.pseg_electricity_consumption_2", hass), new RegExp(EXPIRED));
  assert.match(render(Card, "sensor.pseg_electricity_consumption", hass), new RegExp(EXPIRED));
});

test("a missing second-account entity doesn't fall back to the first account", () => {
  const { Card } = browser();
  const hass = twoAccounts();
  delete hass.states["sensor.pseg_electricity_consumption_2"];

  const html = render(Card, "sensor.pseg_electricity_consumption_2", hass);
  assert.doesNotMatch(html, /1111/);
  assert.match(html, /Waiting for initial data/);
});

test("an unavailable account doesn't show another account's cached reading", () => {
  const { Card } = browser();
  const hass = twoAccounts();
  render(Card, "sensor.pseg_electricity_consumption", hass); // caches the first account

  hass.states["sensor.pseg_electricity_consumption_2"] = { state: "unavailable", attributes: {} };
  const html = render(Card, "sensor.pseg_electricity_consumption_2", hass);
  assert.doesNotMatch(html, /1111/);
});

test("an unavailable account still shows its own cached reading", () => {
  const { Card } = browser();
  const hass = twoAccounts();
  render(Card, "sensor.pseg_electricity_consumption_2", hass);

  hass.states["sensor.pseg_electricity_consumption_2"] = { state: "unavailable", attributes: {} };
  assert.match(render(Card, "sensor.pseg_electricity_consumption_2", hass), /Meter \.\.\.2222/);
});

test("the cache entry older versions shared between all cards is removed", () => {
  const { Card, storage } = browser();
  storage.set("pseg_last_valid_entity", JSON.stringify(reading("11111111")));
  const hass = twoAccounts();
  hass.states["sensor.pseg_electricity_consumption_2"] = { state: "unavailable", attributes: {} };

  assert.doesNotMatch(render(Card, "sensor.pseg_electricity_consumption_2", hass), /1111/);
  assert.equal(storage.has("pseg_last_valid_entity"), false);
});

test("a single account still falls back from an older default entity ID", () => {
  const { Card } = browser();
  const hass = {
    states: {
      "sensor.pseg_electricity_consumption": reading("11111111"),
      "sensor.pseg_connection_status": { state: "auth_required", attributes: {} },
    },
  };

  const html = render(Card, "sensor.pseg_electricity_yesterday", hass);
  assert.match(html, /Meter \.\.\.1111/);
  assert.match(html, new RegExp(EXPIRED));
});

test("without the entity registry the card keeps using the fixed IDs", () => {
  const { Card } = browser();
  const hass = twoAccounts({ firstStatus: "auth_required" });
  delete hass.entities;

  assert.match(render(Card, "sensor.pseg_electricity_consumption", hass), new RegExp(EXPIRED));
});

test("a configured connection_status_entity overrides the lookup", () => {
  const { Card } = browser();
  const hass = twoAccounts({ firstStatus: "connected", secondStatus: "auth_required" });
  const card = new Card();
  card.setConfig({
    entity: "sensor.pseg_electricity_consumption",
    connection_status_entity: "sensor.pseg_connection_status_2",
  });
  card.hass = hass;

  assert.match(card.shadowRoot.innerHTML, new RegExp(EXPIRED));
});
