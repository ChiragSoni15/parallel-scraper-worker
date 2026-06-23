// Home dashboard: operational summary, API-key usage, presets, recent runs.

const Welcome = ({ setRoute, openRun, runs }) => {
  const I = window.Icons;
  const stats = window.useStats ? window.useStats() : null;
  const apiStatus = window.useApiKeys ? window.useApiKeys() : null;
  const totalPlaces = runs.reduce((a, r) => a + (r.found || 0), 0);
  const totalScraped = runs.reduce((a, r) => a + (r.scraped || 0), 0);
  const totalCost = runs.reduce((a, r) => a + (r.cost || 0), 0);
  const runningCount = runs.filter(r => r.status === "running").length;
  const cost1k = totalPlaces > 0 ? (totalCost / totalPlaces * 1000) : 0;

  const apiKeys = apiStatus?.keys || [];
  const activeKeys = apiStatus?.active || 0;
  const exhaustedKeys = apiKeys.filter(k => k.exhausted).length;
  const dayCalls = apiStatus?.day_calls || 0;
  const quotaTotal = apiStatus?.quota_total || 0;
  const quotaUsed = apiStatus?.quota_used || 0;
  const quotaPct = quotaTotal > 0 ? Math.min(100, quotaUsed / quotaTotal * 100) : 0;
  const keyRows = [...apiKeys].sort((a, b) => {
    if (!!a.exhausted !== !!b.exhausted) return a.exhausted ? -1 : 1;
    if (!!a.is_active !== !!b.is_active) return a.is_active ? -1 : 1;
    return (b.day_count || 0) - (a.day_count || 0);
  }).slice(0, 40);
  const fmtCompact = (n) => {
    n = n || 0;
    if (n >= 1000) return (n / 1000).toFixed(n >= 10000 ? 0 : 1) + "K";
    return String(n);
  };

  const onStarter = (preset) => {
    window.__starterQueries = preset.queries;
    setRoute("new");
  };

  return (
    <div className="home-grid">
      <section className="home-head">
        <div>
          <div className="home-kicker">
            {runningCount > 0
              ? <span className="chip dot running">{runningCount} live</span>
              : <span className="chip">idle</span>}
            <span className="chip">{apiStatus ? `${apiStatus.active} active keys` : "loading keys"}</span>
            <span className="chip">{dayCalls.toLocaleString()} calls today</span>
          </div>
          <h1>parallel<span>.</span>scraper</h1>
          <p>Run Google Places discovery, monitor API key capacity, and inspect scraped outputs from one workspace.</p>
        </div>
        <div className="home-actions">
          <button className="btn primary" onClick={() => setRoute("new")}><I.Plus size={13}/> new run</button>
          {runningCount > 0 && <button className="btn ghost" onClick={() => setRoute("live")}><I.Activity size={13}/> watch live</button>}
        </div>
      </section>

      <section className="metric-strip">
        <div className="metric compact"><div className="lbl">runs</div><div className="val">{runs.length}</div><div className="sub">{runningCount} active</div></div>
        <div className="metric compact"><div className="lbl">places found</div><div className="val">{totalPlaces.toLocaleString()}</div><div className="sub">{totalScraped.toLocaleString()} scraped</div></div>
        <div className="metric compact"><div className="lbl">api calls today</div><div className="val">{apiStatus ? dayCalls.toLocaleString() : "-"}</div><div className="sub">IDs-only SKU</div></div>
        <div className="metric compact"><div className="lbl">api spend</div><div className="val">${totalCost.toFixed(2)}</div><div className="sub">${cost1k.toFixed(2)} / 1k</div></div>
        <div className="metric compact"><div className="lbl">disk</div><div className="val">{stats ? (stats.disk_usage_mb || 0) : "-"}<span>MB</span></div><div className="sub">outputs/</div></div>
      </section>

      <section className="panel api-overview">
        <div className="panel-head">
          <I.Database size={13} style={{color:"var(--accent)"}}/>
          <h3>API keys</h3>
          <span className="faint num" style={{marginLeft:"auto", fontSize:11}}>
            {apiStatus ? apiStatus.csv_path : "loading csv"}
          </span>
        </div>
        <div className="api-summary">
          <div>
            <span className="v num">{activeKeys}</span>
            <span>active keys</span>
            <em>{apiStatus?.configured || 0} configured</em>
          </div>
          <div>
            <span className="v num">{dayCalls.toLocaleString()}</span>
            <span>total usage today</span>
            <em>{apiStatus?.day || "billing day"}</em>
          </div>
        </div>
        {quotaTotal > 0 && (
          <div className="api-quota">
            <div className="phase-head">
              <span className="name">quota usage across capped keys</span>
              <span className="pct">{quotaUsed.toLocaleString()} / {quotaTotal.toLocaleString()} ({quotaPct.toFixed(1)}%)</span>
            </div>
            <div className="bar"><span style={{width:`${quotaPct}%`}}/></div>
          </div>
        )}
        {apiKeys.length === 0 ? (
          <div className="api-empty">
            No active API keys were found in inputs/api_keys.csv.
          </div>
        ) : (
          <>
            {exhaustedKeys > 0 && (
              <div className="api-alert">
                <span>{exhaustedKeys} exhausted</span>
              </div>
            )}
            <div className="api-key-table">
              {keyRows.map(k => {
                const unlimited = !k.quota;
                const pct = unlimited ? 100 : Math.min(100, (k.day_count || 0) / k.quota * 100);
                const cls = !k.is_active ? "disabled" : unlimited ? "unlimited" : k.exhausted ? "full" : pct >= 80 ? "warn" : "";
                return (
                  <div key={k.alias || k.key_id} className={`api-key-row ${k.exhausted ? "exhausted" : ""} ${!k.is_active ? "disabled" : ""}`}>
                    <span className="api-name">{k.alias || `key ${k.key_id}`}</span>
                    <span className={`keybar ${cls}`}><span style={{width: `${pct}%`}}/></span>
                    <span className="api-count num">{(k.day_count || 0).toLocaleString()}{unlimited ? "" : `/${fmtCompact(k.quota)}`}</span>
                    <span className="api-run num">{k.is_active ? "active" : "off"}</span>
                  </div>
                );
              })}
            </div>
          </>
        )}
      </section>

      <aside className="home-side">
        <div className="panel preset-panel">
          <div className="panel-head">
            <h3>start from preset</h3>
          </div>
          <div className="panel-body preset-list">
            {(window.STARTER_PRESETS || []).map((p, i) => {
              const ic = i === 0 ? <I.Bolt/> : i === 1 ? <I.Star/> : i === 2 ? <I.Cpu/> : <I.Layers/>;
              return (
                <button key={p.key} className="preset-card" onClick={() => onStarter(p)}>
                  <span className="pco">{ic}</span>
                  <span style={{flex:1, minWidth:0}}>
                    <span className="ptit">{p.title}</span>
                    <div className="psub">{p.sub}</div>
                  </span>
                  <I.ChevronRight size={12} style={{color:"var(--fg-3)", marginTop:6}}/>
                </button>
              );
            })}
          </div>
        </div>
      </aside>
    </div>
  );
};

window.Welcome = Welcome;
