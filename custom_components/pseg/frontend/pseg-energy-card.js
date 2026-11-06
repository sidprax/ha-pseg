/**
 * PSEG Energy Card - Ultra-Minimal Galleria Edition
 *
 * Requirements & Features:
 * - 2 Rows total (tight gap, perfectly aligned header):
 *   - Row 1: Left meta (As of MM/DD, Meter ...XXXX) + Right horizontal mini stat items (Latest with ↑/↓ % pill, Avg, Proj with vs last cycle %, Rate)
 *   - Row 2: Compact bar chart (7d or 14d governed by separator toggle `input_boolean.pseg_chart_14d` or config `days`)
 * - Color-coded bars relative to average:
 *   - Green to Amber scheme (no harsh red):
 *     - Below avg (< -5%): Soft emerald green
 *     - At/near avg (within ±5%): Amber-yellow
 *     - Above avg (> +5%): Warm amber/orange
 * - Continuous daily timeline: detects and fills missing calendar dates (e.g. 10/10) with 0 / N/A dotted slot so all dates are represented.
 * - Bar spacing reduced: bars take wider width (up to 28px max-width) with small 3px gaps.
 * - Values inside (<10: 1 decimal, >=10: rounded); when in dollar mode, bars display numbers without '$' sign.
 * - Projection shows vs last cycle diff badge (e.g. +5% or -5%).
 * - Perfectly aligned mini stat items: fixed height labels, baseline-aligned values.
 * - Tapping anywhere opens detailed popup (`#pseg_energy_popup`).
 */

// Older default entity IDs the card falls back to when it is set to one of them.
const LEGACY_ENTITY_IDS = [
  "sensor.pseg_electricity_consumption",
  "sensor.pseg_electricity_yesterday",
  "sensor.pseg_smart_energy_meter_electricity_yesterday",
];
// Older versions of the card shared one cache entry, whichever account it showed.
const SHARED_CACHE_KEY = "pseg_last_valid_entity";

function escapeHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

class PsegEnergyCard extends HTMLElement {
  static getStubConfig() {
    return {
      entity: "sensor.pseg_electricity_consumption",
    };
  }

  constructor() {
    super();
  }

  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("PSEG Energy Card: Please specify an entity");
    }
    this.config = {
      cost_per_kwh: null,
      cycle_days: 30,
      days: 14,
      last_cycle_kwh: null,
      toggle_entity: "input_boolean.pseg_display_dollars",
      range_toggle_entity: "input_boolean.pseg_chart_14d",
      next_update_entity: null,
      connection_status_entity: null,
      show_next_update: false,
      popup_hash: null,
      ...config
    };
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
    }
    if (this._hass) {
      this.render();
    }
  }

  set hass(hass) {
    const oldHass = this._hass;
    this._hass = hass;
    if (!this.config) return;

    if (oldHass && oldHass.states && hass.states) {
      const e1 = this.config.entity;
      const e2 = this.config.toggle_entity;
      const e3 = this.config.range_toggle_entity;
      const e4 = this.config.next_update_entity || this._accountEntity("next_update");
      const e5 = this.config.connection_status_entity || this._accountEntity("connection_status");

      const sameE1 = !e1 || oldHass.states[e1] === hass.states[e1];
      const sameE2 = !e2 || oldHass.states[e2] === hass.states[e2];
      const sameE3 = !e3 || oldHass.states[e3] === hass.states[e3];
      const sameE4 = !e4 || oldHass.states[e4] === hass.states[e4];
      const sameE5 = !e5 || !hass.states[e5] || oldHass.states[e5] === hass.states[e5];

      if (sameE1 && sameE2 && sameE3 && sameE4 && sameE5) {
        return;
      }
    }

    this.render();
  }

  connectedCallback() {
    if (this._hass && this.config) {
      this.render();
    }
  }

  getCardSize() {
    return 2;
  }

  // Find this account's own copy of a PSE&G sensor such as "connection_status".
  // A second account's sensors get a "_2" suffix, so the fixed ID would show
  // the first account's status on the second account's card.
  _accountEntity(key) {
    const fixedId = `sensor.pseg_${key}`;
    const entities = this._hass && this._hass.entities;
    const own = entities && entities[this._entityId || this.config.entity];
    if (!own || !own.device_id) return fixedId;
    const pattern = new RegExp(`^sensor\\.pseg_${key}(_\\d+)?$`);
    const match = Object.values(entities).find(
      (e) => e.device_id === own.device_id && pattern.test(e.entity_id)
    );
    return match ? match.entity_id : null;
  }

  getGridOptions() {
    return {
      columns: 12,
      min_columns: 6,
      max_columns: 12,
    };
  }

  openPopup() {
    if (this.config.popup_hash) {
      window.location.hash = this.config.popup_hash;
    } else {
      const event = new CustomEvent("hass-more-info", {
        bubbles: true,
        composed: true,
        detail: { entityId: this.config.entity }
      });
      this.dispatchEvent(event);
    }
  }

  // Format value for bar labels (never show $ sign in bars)
  formatBarVal(item, isDollars, rate) {
    if (isDollars) {
      const d = (item && item.cost != null && item.cost > 0) ? item.cost : (((item && item.kwh) || 0) * rate);
      return d < 10 ? d.toFixed(1) : `${Math.round(d)}`;
    }
    const kwh = (item && item.kwh != null) ? item.kwh : 0;
    return kwh < 10 ? kwh.toFixed(1) : `${Math.round(kwh)}`;
  }

  // Format value for header stats (clean numbers only, unit is indicated in the column label)
  formatPrecision(kwh, isDollars, rate) {
    if (isDollars) {
      const d = kwh * rate;
      return d.toFixed(2);
    }
    return kwh.toFixed(2);
  }

  // Green to Amber gradient (smooth transition without aggressive red)
  getBarColor(kwh, avgKwh) {
    if (!avgKwh || avgKwh <= 0) {
      return "linear-gradient(180deg, rgba(16, 185, 129, 0.95) 0%, rgba(16, 185, 129, 0.50) 100%)";
    }
    const ratio = kwh / avgKwh;
    if (ratio < 0.95) {
      // Below avg -> Emerald Green
      return "linear-gradient(180deg, rgba(16, 185, 129, 0.95) 0%, rgba(16, 185, 129, 0.45) 100%)";
    } else if (ratio <= 1.12) {
      // Near avg -> Warm Yellow / Amber
      return "linear-gradient(180deg, rgba(234, 179, 8, 0.95) 0%, rgba(234, 179, 8, 0.50) 100%)";
    } else {
      // Higher than avg -> Warm Amber / Orange (soft amber, not red)
      return "linear-gradient(180deg, rgba(245, 158, 11, 0.95) 0%, rgba(217, 119, 6, 0.55) 100%)";
    }
  }

  render() {
    if (!this.shadowRoot || !this._hass || !this.config) return;

    try {
      this._entityId = this.config.entity;
      let entity = this._hass.states[this._entityId];
      // Only fall back to the other default IDs when the card is set to one of
      // them: a card for a second account must never show the first account.
      if (!entity && LEGACY_ENTITY_IDS.includes(this.config.entity)) {
        const fallbackId = LEGACY_ENTITY_IDS.find((id) => this._hass.states[id]);
        if (fallbackId) {
          this._entityId = fallbackId;
          entity = this._hass.states[fallbackId];
        }
      }

      // Each card caches its own entity, so an unavailable account can't show another's data
      const cacheKey = `${SHARED_CACHE_KEY}:${this.config.entity}`;
      const liveConnectionEnt = this.config.connection_status_entity || this._accountEntity("connection_status");
      const liveConnectionStatus = liveConnectionEnt ? this._hass.states[liveConnectionEnt]?.state : undefined;
      if (entity?.attributes?.consumption_data_available === false &&
          entity.attributes.auth_required !== true && liveConnectionStatus !== "auth_required") {
        this.shadowRoot.innerHTML = `
          <ha-card style="padding: 20px;">
            <strong>PSE&amp;G Energy</strong>
            <p>No hourly consumption data received.</p>
            <p>The portal responded, but no usage readings could be imported. Check the integration diagnostics.</p>
          </ha-card>`;
        return;
      }
      try {
        localStorage.removeItem(SHARED_CACHE_KEY);
      } catch (_) {}

      // 1. Cache valid entity attributes if present
      if (entity && entity.attributes && Array.isArray(entity.attributes.history_14d) && entity.attributes.history_14d.length > 0) {
        this._cachedEntity = {
          state: entity.state,
          attributes: { ...entity.attributes }
        };
        try {
          localStorage.setItem(cacheKey, JSON.stringify(this._cachedEntity));
        } catch (_) {}
      }

      // 2. Fall back to cached entity if live entity is unavailable, unknown, or missing
      const isUnavailable = !entity || entity.state === "unavailable" || entity.state === "unknown";
      if (isUnavailable) {
        if (!this._cachedEntity) {
          try {
            const stored = localStorage.getItem(cacheKey);
            if (stored) this._cachedEntity = JSON.parse(stored);
          } catch (_) {}
        }
        if (this._cachedEntity) {
          entity = {
            state: this._cachedEntity.state,
            attributes: { ...this._cachedEntity.attributes }
          };
        }
      }

      if (!entity || !entity.attributes || !entity.attributes.history_14d || entity.attributes.history_14d.length === 0) {
        this.shadowRoot.innerHTML = `
          <ha-card style="background: rgba(25, 25, 25, 0.45); backdrop-filter: blur(12px); -webkit-backdrop-filter: blur(12px); border-radius: 18px; border: 1px solid rgba(255, 255, 255, 0.08); padding: 16px 20px; display: flex; align-items: center; justify-content: space-between; color: rgba(255, 255, 255, 0.7); font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', Roboto, sans-serif; font-size: 13px;">
            <div style="display: flex; align-items: center;">
              <span style="width: 8px; height: 8px; border-radius: 50%; background: #f59e0b; display: inline-block; margin-right: 10px; box-shadow: 0 0 8px #f59e0b;"></span>
              <div>
                <div style="color: #ffffff; font-weight: 600;">PSE&amp;G Energy</div>
                <div style="font-size: 11px; color: rgba(255, 255, 255, 0.45); margin-top: 2px;">Waiting for initial data sync...</div>
              </div>
            </div>
            <span style="font-size: 11px; color: rgba(255,255,255,0.4);">Connecting</span>
          </ha-card>
        `;
        return;
      }

      const connectionEnt = this.config.connection_status_entity || this._accountEntity("connection_status");
      const connectionStatus = connectionEnt ? this._hass.states[connectionEnt]?.state : undefined;
      const isAuthRequired = isUnavailable ||
                            entity.attributes.auth_required === true ||
                            connectionStatus === "auth_required";

    const toggleEnt = this.config.toggle_entity || "input_boolean.pseg_display_dollars";
    const isDollars = this._hass.states[toggleEnt]?.state === "on";

    const rangeToggleEnt = this.config.range_toggle_entity || "input_boolean.pseg_chart_14d";
    let daysToShow = parseInt(this.config.days) || 14;
    if (this._hass.states[rangeToggleEnt]) {
      daysToShow = this._hass.states[rangeToggleEnt].state === "on" ? 14 : 7;
    }

    // Derive electric rate from entity attributes or user config
    let defaultRate = 0.247;
    if (entity.attributes.rate_applied != null && !isNaN(parseFloat(entity.attributes.rate_applied))) {
      defaultRate = parseFloat(entity.attributes.rate_applied);
    } else if (entity.attributes.rate_7d_avg != null && !isNaN(parseFloat(entity.attributes.rate_7d_avg))) {
      defaultRate = parseFloat(entity.attributes.rate_7d_avg);
    } else {
      const latestKwhVal = parseFloat(entity.attributes.latest_kwh) || parseFloat(entity.state) || 0;
      const latestCostVal = parseFloat(entity.attributes.latest_cost) || 0;
      if (latestKwhVal > 0 && latestCostVal > 0) {
        defaultRate = parseFloat((latestCostVal / latestKwhVal).toFixed(3));
      }
    }
    const rate = (this.config.cost_per_kwh != null && !isNaN(parseFloat(this.config.cost_per_kwh)))
      ? parseFloat(this.config.cost_per_kwh)
      : defaultRate;

    const rateSource = entity.attributes.rate_source || "derived";
    const daysUsedForRate = parseInt(entity.attributes.days_used_for_rate, 10) || 0;
    let rateTooltip = `$${rate.toFixed(3)}/kWh`;
    if (this.config.cost_per_kwh != null) {
      rateTooltip += " (Card config override)";
    } else if (rateSource === "user_configured") {
      rateTooltip += " (Integration options override)";
    } else if (rateSource === "derived_7d_average") {
      rateTooltip += ` (7-day weighted average from ${daysUsedForRate} qualifying days of portal billing)`;
    } else {
      rateTooltip += " (Default estimate)";
    }

    const cycleDays = parseInt(this.config.cycle_days) || 30;
    const hasLastCycle = this.config.last_cycle_kwh != null && !isNaN(parseFloat(this.config.last_cycle_kwh));
    const lastCycleKwh = hasLastCycle ? parseFloat(this.config.last_cycle_kwh) : 0;

    const rawHistory = entity.attributes.history_14d || [];
    const latestRawDate = entity.attributes.latest_date || "";
    const meterRaw = entity.attributes.meter || "";

    // Extract Meter last 4 digits
    const meterNumMatch = meterRaw.match(/\d+/);
    const fullMeterNum = meterNumMatch ? meterNumMatch[0] : "";
    const meterLast4 = fullMeterNum.length >= 4 ? fullMeterNum.slice(-4) : fullMeterNum;
    const meterText = meterLast4 ? `Meter ...${meterLast4}` : "Electric";

    // Reconstruct calendar sequence to avoid missing dates (e.g. 10/10)
    const historyMap = new Map();
    rawHistory.forEach(item => {
      if (item && item.date) {
        historyMap.set(item.date, {
          kwh: parseFloat(item.kwh) || 0,
          cost: item.cost != null ? parseFloat(item.cost) : null,
          isExactCost: !!item.is_exact_cost
        });
      }
    });

    // Reference end date: yesterday (since meter readings are delayed by 1 day)
    const now = new Date();
    const yesterday = new Date(now.getTime() - 86400000);
    const yesterdayIso = yesterday.toISOString().slice(0, 10);

    let endDate = yesterdayIso;
    if (latestRawDate && latestRawDate > endDate) {
      endDate = latestRawDate;
    }
    if (!endDate && rawHistory.length > 0) {
      endDate = rawHistory[rawHistory.length - 1].date;
    }
    if (!endDate) {
      endDate = new Date().toISOString().slice(0, 10);
    }

    const endDt = new Date(endDate + "T12:00:00Z");
    const activeData = [];
    for (let i = daysToShow - 1; i >= 0; i--) {
      const cur = new Date(endDt.getTime() - i * 86400000);
      const iso = cur.toISOString().slice(0, 10);
      const itemData = historyMap.get(iso);
      const val = itemData ? itemData.kwh : null;
      const costVal = itemData ? itemData.cost : null;
      const isExactCost = itemData ? itemData.isExactCost : false;
      activeData.push({
        date: iso,
        kwh: val,
        cost: costVal,
        isExactCost: isExactCost,
        isMissing: val === null
      });
    }

    // Format As of MM/DD header date
    let asOfDateStr = latestRawDate;
    const lParts = latestRawDate.split("-");
    if (lParts.length === 3) {
      asOfDateStr = `${lParts[1]}/${lParts[2]}`;
    }

    // Format Next Update timestamp
    let nextUpdateInline = "";
    let nextUpdateTooltip = "";
    const nextUpdateEntId = this.config.next_update_entity || this._accountEntity("next_update");
    const nextEnt = nextUpdateEntId ? this._hass.states[nextUpdateEntId] : undefined;
    const nextVal = (nextEnt && nextEnt.state) ? nextEnt.state : (entity.attributes.next_update || "");
    if (nextVal && nextVal !== "unknown" && nextVal !== "unavailable") {
      try {
        const nextDt = new Date(nextVal);
        if (!isNaN(nextDt.getTime())) {
          const now = new Date();
          const isToday = nextDt.getDate() === now.getDate() && nextDt.getMonth() === now.getMonth() && nextDt.getFullYear() === now.getFullYear();
          const timeStr = nextDt.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
          const fullStr = isToday ? timeStr : `${nextDt.getMonth() + 1}/${nextDt.getDate()} ${timeStr}`;
          nextUpdateTooltip = `Next pull: ${fullStr}`;
          if (this.config.show_next_update) {
            nextUpdateInline = ` • ↻ ${fullStr}`;
          }
        }
      } catch (e) {}
    }

    // Calculate Average over valid points
    const validPoints = activeData.filter(d => !d.isMissing);
    const count = validPoints.length || 1;
    const avgKwh = validPoints.reduce((acc, c) => acc + (c.kwh || 0), 0) / count;

    // Latest day data
    const lastValidPoint = validPoints[validPoints.length - 1] || { kwh: parseFloat(entity.state) || 0 };
    const latestKwh = parseFloat(lastValidPoint.kwh) || 0;

    // Latest vs Average Diff %
    const diffPct = avgKwh > 0 ? ((latestKwh - avgKwh) / avgKwh) * 100 : 0;
    const isHigher = diffPct > 0;
    const absDiff = Math.abs(diffPct).toFixed(0);
    const arrowSymbol = isHigher ? "↑" : "↓";
    const pillClass = isHigher ? "pill-amber" : "pill-green";

    // Cycle Projection & comparison vs last cycle
    const projKwh = avgKwh * cycleDays;
    let projDiffBadge = "";
    if (hasLastCycle && lastCycleKwh > 0) {
      const projDiffPct = ((projKwh - lastCycleKwh) / lastCycleKwh) * 100;
      const isProjHigher = projDiffPct > 0;
      const absProjDiff = Math.abs(projDiffPct).toFixed(0);
      const projArrowSymbol = isProjHigher ? "↑" : "↓";
      const projPillClass = isProjHigher ? "pill-amber" : "pill-green";
      projDiffBadge = `
        <span class="diff-pill ${projPillClass}" title="${projDiffPct >= 0 ? '+' : ''}${projDiffPct.toFixed(0)}% vs last cycle">
          ${projArrowSymbol}${absProjDiff}%
        </span>
      `;
    }

    // Single-unit string representations for header mini stats
    const latestCost = parseFloat(entity.attributes.latest_cost) || (latestKwh * rate);
    const latestDisplay = isDollars ? latestCost.toFixed(2) : latestKwh.toFixed(2);
    const avgCost = validPoints.reduce((acc, c) => acc + ((c.cost != null && c.cost > 0) ? c.cost : (c.kwh * rate)), 0) / count;
    const avgDisplay = isDollars ? avgCost.toFixed(2) : avgKwh.toFixed(2);
    const projDisplay = isDollars
      ? `${Math.round(projKwh * rate)}`
      : `${Math.round(projKwh)}`;
    const unitLabel = isDollars ? "$" : "kWh";
    const rateDisplay = rate.toFixed(2);

    // Build bars with reduced gap and wider fill
    const maxKwh = Math.max(...validPoints.map(d => d.kwh || 0), 6);
    const barsHtml = activeData.map((item, idx) => {
      const kwh = item.kwh !== null ? item.kwh : 0;
      const heightPct = item.isMissing ? 4 : Math.min(Math.max((kwh / maxKwh) * 100, 16), 100);
      const isLatest = idx === activeData.length - 1 && !item.isMissing;
      const barValText = item.isMissing ? "-" : this.formatBarVal(item, isDollars, rate);
      const barBg = item.isMissing ? "rgba(255, 255, 255, 0.12)" : this.getBarColor(kwh, avgKwh);

      // Compact date (M/D)
      const dParts = item.date.split("-");
      const dateText = dParts.length === 3 ? `${parseInt(dParts[1])}/${parseInt(dParts[2])}` : item.date;
      const fitsInside = heightPct >= 32;
      const dayCostText = (item.cost != null && item.cost > 0) ? `$${item.cost.toFixed(2)}` : `$${(kwh * rate).toFixed(2)}`;
      const barTitle = item.isMissing ? `${item.date}: No Data` : `${item.date}: ${kwh} kWh • ${dayCostText}`;

      return `
        <div class="bar-col ${isLatest ? 'is-latest' : ''} ${item.isMissing ? 'is-missing' : ''}" title="${escapeHtml(barTitle)}">
          <div class="bar-track">
            <div class="bar-fill" style="height: ${heightPct}%; background: ${barBg};">
              ${!item.isMissing && fitsInside ? `<span class="val-inside">${escapeHtml(barValText)}</span>` : ""}
            </div>
            ${!item.isMissing && !fitsInside ? `<span class="val-above" style="bottom: ${heightPct + 2}%;">${escapeHtml(barValText)}</span>` : ""}
          </div>
          <span class="bar-date">${escapeHtml(dateText)}</span>
        </div>
      `;
    }).join("");

    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block;
          font-family: var(--primary-font-family, 'Montserrat', sans-serif);
        }
        ha-card {
          position: relative;
          background: rgba(25, 25, 25, 0.40) !important;
          backdrop-filter: blur(10px) !important;
          -webkit-backdrop-filter: blur(10px) !important;
          border-radius: 18px !important;
          border: 1px solid rgba(255, 255, 255, 0.08) !important;
          box-shadow: 0 4px 20px 0 rgba(0, 0, 0, 0.25) !important;
          padding: 10px 14px 8px;
          color: #ffffff;
          overflow: hidden;
          box-sizing: border-box;
          cursor: pointer;
          transition: transform 0.15s ease, border-color 0.15s ease;
        }
        ha-card:hover {
          border-color: rgba(255, 255, 255, 0.16) !important;
        }
        ha-card:active {
          transform: scale(0.995);
        }

        /* ROW 1: TOP ROW (Left Meta + Right Single-Line Mini Stats) */
        .top-row {
          display: flex;
          justify-content: space-between;
          align-items: flex-end;
          gap: 10px;
          margin-bottom: 2px;
          padding-bottom: 0px;
        }

        /* Left Meta */
        .meta-col {
          display: flex;
          flex-direction: column;
          justify-content: flex-end;
          flex-shrink: 0;
          padding-bottom: 1px;
        }
        .meta-date {
          font-size: 13.5px;
          font-weight: 700;
          color: #ffffff;
          letter-spacing: -0.2px;
          line-height: 1.15;
          white-space: nowrap;
        }
        .meta-meter {
          font-size: 11px;
          font-weight: 500;
          color: rgba(255, 255, 255, 0.50);
          letter-spacing: 0.1px;
          margin-top: 2px;
          line-height: 1.1;
          white-space: nowrap;
        }

        /* Right Metrics Strip */
        .metrics-inline {
          display: flex;
          align-items: flex-end;
          gap: 12px;
          overflow-x: auto;
          scrollbar-width: none;
        }
        .metrics-inline::-webkit-scrollbar {
          display: none;
        }

        .stat-item {
          display: flex;
          flex-direction: column;
          align-items: flex-end;
          justify-content: flex-end;
        }
        .stat-label {
          font-size: 9px;
          font-weight: 600;
          text-transform: uppercase;
          color: rgba(255, 255, 255, 0.45);
          letter-spacing: 0.35px;
          line-height: 1;
          margin-bottom: 4px;
          white-space: nowrap;
          height: 9px;
        }
        .stat-val-row {
          display: flex;
          align-items: center;
          gap: 3px;
          height: 18px;
        }
        .stat-val {
          font-size: 13px;
          font-weight: 700;
          color: #ffffff;
          letter-spacing: -0.2px;
          line-height: 1;
        }

        /* Pill Badges for Higher/Lower */
        .diff-pill {
          display: inline-flex;
          align-items: center;
          gap: 1px;
          padding: 1px 4px;
          border-radius: 9px;
          font-size: 9.5px;
          font-weight: 700;
          line-height: 1.15;
        }
        .pill-green {
          background: rgba(16, 185, 129, 0.18);
          color: #34d399;
          border: 1px solid rgba(16, 185, 129, 0.35);
        }
        .pill-amber {
          background: rgba(245, 158, 11, 0.18);
          color: #fbbf24;
          border: 1px solid rgba(245, 158, 11, 0.35);
        }

        /* ROW 2: COMPACT BAR CHART */
        .chart-row {
          display: flex;
          align-items: flex-end;
          gap: 3px;
          height: 62px;
          padding-top: 10px;
          box-sizing: border-box;
        }
        .bar-col {
          flex: 1;
          display: flex;
          flex-direction: column;
          align-items: center;
          height: 100%;
          min-width: 0;
          position: relative;
        }
        .bar-track {
          flex: 1;
          width: 100%;
          position: relative;
          display: flex;
          align-items: flex-end;
          justify-content: center;
        }
        .bar-fill {
          width: 100%;
          max-width: 28px;
          border-radius: 4px 4px 2px 2px;
          position: relative;
          display: flex;
          justify-content: center;
          align-items: flex-start;
          padding-top: 2px;
          box-sizing: border-box;
          transition: height 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }
        .bar-col.is-latest .bar-fill {
          outline: 1.5px solid rgba(255, 255, 255, 0.45);
          outline-offset: 1px;
        }
        .bar-col.is-missing .bar-fill {
          border: 1px dashed rgba(255, 255, 255, 0.20);
        }

        .val-inside {
          font-size: 8.5px;
          font-weight: 700;
          color: #ffffff;
          line-height: 1;
          text-shadow: 0 1px 2px rgba(0, 0, 0, 0.8);
          pointer-events: none;
        }
        .val-above {
          position: absolute;
          font-size: 8px;
          font-weight: 700;
          color: rgba(255, 255, 255, 0.75);
          line-height: 1;
          pointer-events: none;
          text-align: center;
          white-space: nowrap;
        }
        .bar-date {
          font-size: 8.5px;
          font-weight: 500;
          color: rgba(255, 255, 255, 0.45);
          margin-top: 3px;
          line-height: 1;
          white-space: nowrap;
        }
        .bar-col.is-latest .bar-date {
          color: #ffffff;
          font-weight: 700;
        }
      </style>

      <ha-card id="cardWrapper" title="Click for details & meter history">
        ${isAuthRequired ? `
          <div id="psegAuthWarning" style="
            display: flex;
            align-items: center;
            justify-content: space-between;
            background: rgba(245, 158, 11, 0.16);
            border: 1px solid rgba(245, 158, 11, 0.38);
            border-radius: 9px;
            padding: 5px 9px;
            margin-bottom: 8px;
            cursor: pointer;
            transition: background 0.2s ease;
          ">
            <div style="display: flex; align-items: center; gap: 6px;">
              <span style="font-size: 13px; line-height: 1;">⚠️</span>
              <span style="font-size: 11px; font-weight: 600; color: #fbbf24; letter-spacing: -0.1px;">
                Session expired &bull; Re-authentication required
              </span>
            </div>
            <span style="
              font-size: 9.5px;
              font-weight: 700;
              color: #fbbf24;
              background: rgba(245, 158, 11, 0.25);
              border-radius: 5px;
              padding: 2px 6px;
              text-transform: uppercase;
              letter-spacing: 0.3px;
            ">Reconfigure</span>
          </div>
        ` : ''}

        <!-- Row 1: Left Meta + Right Single-Line Mini Stats -->
        <div class="top-row">
          <div class="meta-col">
            <span class="meta-date" title="${escapeHtml(nextUpdateTooltip || `As of ${asOfDateStr}`)}">As of ${escapeHtml(asOfDateStr)}</span>
            <span class="meta-meter">${escapeHtml(meterText)}${escapeHtml(nextUpdateInline)}</span>
          </div>

          <div class="metrics-inline">
            <!-- Latest + Higher/Lower Arrow Pill -->
            <div class="stat-item">
              <span class="stat-label">Usage (${unitLabel})</span>
              <div class="stat-val-row">
                <span class="stat-val">${latestDisplay}</span>
                <span class="diff-pill ${pillClass}">
                  ${arrowSymbol}${absDiff}%
                </span>
              </div>
            </div>

            <!-- Avg -->
            <div class="stat-item">
              <span class="stat-label">Avg (${daysToShow}d)</span>
              <div class="stat-val-row">
                <span class="stat-val">${avgDisplay}</span>
              </div>
            </div>

            <!-- Projection (Cycle) -->
            <div class="stat-item">
              <span class="stat-label">Proj (Cycle)</span>
              <div class="stat-val-row">
                <span class="stat-val">${projDisplay}</span>
                ${projDiffBadge}
              </div>
            </div>

            <!-- Rate -->
            <div class="stat-item" title="${escapeHtml(rateTooltip)}">
              <span class="stat-label">Rate</span>
              <div class="stat-val-row">
                <span class="stat-val">${rateDisplay}</span>
              </div>
            </div>
          </div>
        </div>

        <!-- Row 2: Compact Bar Chart Colored Against Avg -->
        <div class="chart-row">
          ${barsHtml}
        </div>
      </ha-card>
    `;

    // Click handler for auth warning banner
    const authBanner = this.shadowRoot.getElementById("psegAuthWarning");
    if (authBanner) {
      authBanner.addEventListener("click", (e) => {
        e.stopPropagation();
        window.location.href = "/config/integrations";
      });
    }

    // Click handler for popup
    this.shadowRoot.getElementById("cardWrapper")?.addEventListener("click", () => {
      this.openPopup();
    });
    } catch (err) {
      console.error("PSEG Energy Card render error:", err);
      this.shadowRoot.innerHTML = `
        <ha-card style="background: rgba(25, 25, 25, 0.4); backdrop-filter: blur(10px); padding: 14px 18px; border-radius: 18px; color: #ff6b6b; border: 1px solid rgba(255,255,255,0.08); font-family: -apple-system, BlinkMacSystemFont, 'SF Pro Display', Roboto, sans-serif; font-size: 13px;">
          PSE&amp;G Card Error: ${escapeHtml(err.message || String(err))}
        </ha-card>
      `;
    }
  }
}

if (!customElements.get("pseg-energy-card")) {
  customElements.define("pseg-energy-card", PsegEnergyCard);
}
window.customCards = window.customCards || [];
if (!window.customCards.some(c => c.type === "pseg-energy-card")) {
  window.customCards.push({
    type: "pseg-energy-card",
    name: "PSEG Energy Card (Ultra-Minimal)",
    preview: true,
    description: "Ultra-compact minimal Galleria energy card with smooth green-amber bars and single-line metrics."
  });
}
