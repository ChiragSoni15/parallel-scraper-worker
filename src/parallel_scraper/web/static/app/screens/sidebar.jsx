// Sidebar: left navigation rail with collapsible compact mode.

const Sidebar = ({
  route,
  setRoute,
  openRun,
  runs,
  currentRunId,
  onRefresh,
  collapsed,
  onToggleCollapsed,
}) => {
  const I = window.Icons;
  return (
    <aside className="rail">
      <div className="rail-head">
        <span className="brand-mark"><I.Brand /></span>
        <span className="brand-name">parallel<b>.</b>scraper</span>
        <button
          className="btn ghost icon sm rail-collapse"
          title={collapsed ? "expand sidebar" : "collapse sidebar"}
          onClick={() => onToggleCollapsed && onToggleCollapsed()}
        >
          {collapsed ? <I.ChevronRight size={11}/> : <I.ChevronLeft size={11}/>}
        </button>
      </div>

      <div className="rail-section">
        <button
          className="rail-item"
          aria-current={route === "welcome" ? "page" : undefined}
          title="all runs"
          onClick={() => setRoute("welcome")}
        >
          <I.ListSquare className="ico" />
          <span className="rail-text">all runs</span>
          <span style={{marginLeft:"auto"}} className="num faint rail-extra">{runs.length}</span>
        </button>
        <button
          className="rail-item"
          aria-current={route === "new" ? "page" : undefined}
          title="new run"
          onClick={() => setRoute("new")}
        >
          <I.Plus className="ico" />
          <span className="rail-text">new run</span>
          <span style={{marginLeft:"auto"}} className="kbd rail-extra">N</span>
        </button>
      </div>

      <div className="rail-section rail-runs">
        <div className="rail-label">
          <span>recent runs</span>
          <button className="btn ghost icon sm rail-refresh" title="refresh" onClick={() => onRefresh && onRefresh()}>
            <I.Refresh size={11}/>
          </button>
        </div>
        <div className="runlist">
          {runs.length === 0 ? (
            <div className="runlist-empty">no runs yet</div>
          ) : runs.map(r => (
            <button
              key={r.id}
              className={`run-tile ${r.status}`}
              title={`${r.name} - ${r.id}`}
              aria-current={r.id === currentRunId ? "page" : undefined}
              onClick={() => {
                // Always route to the Live screen first — it shows the run summary
                // + resume button + a 'results' button to drill into the lead table.
                // Going straight to Results on completed runs was hiding the resume
                // affordance and forcing a heavy phase2_data.csv load on first click.
                openRun ? openRun(r) : setRoute("live", r.id);
              }}
            >
              <span className="dot" />
              <span className="meta">
                <span className="meta-name">{r.name}</span>
                <span className="meta-sub">{r.id} - {(r.found || 0).toLocaleString()} places</span>
              </span>
              <span className="meta-time">{r.started}</span>
            </button>
          ))}
        </div>
      </div>

      <div className="rail-foot">
        <span>v0.2 - local-first</span>
        <span className="num faint">{runs.length} runs</span>
      </div>
    </aside>
  );
};

window.Sidebar = Sidebar;
