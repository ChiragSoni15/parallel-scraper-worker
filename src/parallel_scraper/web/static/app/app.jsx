// Main App — routing, theming, tweaks panel wiring.
// Hash format:  #welcome | #new | #live[/<runId>] | #results[/<runId>]
// Plus tweak overrides via #...&theme=light&accent=cyan&map=satellite

const { useState: useStateApp, useEffect: useEffectApp, useMemo: useMemoApp } = React;

const TWEAK_DEFAULTS = {
  theme: "dark",
  accent: "lime",
  density: "comfortable",
  mapStyle: "dark",
};

const ACCENTS = {
  lime:    { c: "oklch(0.78 0.16 145)", fg: "oklch(0.18 0.04 145)" },
  amber:   { c: "oklch(0.78 0.16 75)",  fg: "oklch(0.18 0.04 75)"  },
  cyan:    { c: "oklch(0.78 0.13 210)", fg: "oklch(0.18 0.04 210)" },
  magenta: { c: "oklch(0.72 0.18 340)", fg: "oklch(0.18 0.04 340)" },
  indigo:  { c: "oklch(0.65 0.20 270)", fg: "oklch(0.99 0.01 270)" },
};

const WORKSPACE_TABS_KEY = "parallel_scraper_workspace_tabs_v1";

function parseHash() {
  const raw = (location.hash || "").replace(/^#/, "");
  // legacy "#/run/<id>" → "live/<id>"
  const legacy = raw.match(/^\/?run\/([^&/]+)/);
  if (legacy) return { name: "live", runId: decodeURIComponent(legacy[1]), params: {} };
  const [pathPart = "", paramsPart = ""] = raw.split("&", 2).length === 2 ? [raw.split("&")[0], raw.split("&").slice(1).join("&")] : [raw, ""];
  const segs = pathPart.split("/").filter(Boolean);
  const name = ["welcome", "new", "live", "results", "all"].includes(segs[0]) ? segs[0] : "welcome";
  const runId = segs[1] ? decodeURIComponent(segs[1]) : null;
  const params = {};
  for (const kv of paramsPart.split("&")) {
    const [k, v] = kv.split("=");
    if (k) params[k] = decodeURIComponent(v || "");
  }
  return { name, runId, params };
}

function buildHash(name, runId, params) {
  let h = `#${name}`;
  if (runId) h += `/${encodeURIComponent(runId)}`;
  if (params && Object.keys(params).length) {
    h += "&" + Object.entries(params).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&");
  }
  return h;
}

function tabKey(name, runId) {
  return `${name}:${runId || ""}`;
}

function tabForRoute(name, runId, runs = []) {
  const route = name === "all" ? "welcome" : name;
  const run = runId ? runs.find(r => r.id === runId) : null;
  if (route === "welcome") {
    return { key: tabKey("welcome", ""), name: "welcome", runId: null, title: "All runs", kind: "home" };
  }
  if (route === "new") {
    return { key: tabKey("new", ""), name: "new", runId: null, title: "New run", kind: "new" };
  }
  if ((route === "live" || route === "results") && runId) {
    const short = run ? (run.area || run.name || runId) : runId;
    return {
      key: tabKey(route, runId),
      name: route,
      runId,
      title: route === "live" ? `Live: ${short}` : `Results: ${short}`,
      kind: route,
    };
  }
  return { key: tabKey("welcome", ""), name: "welcome", runId: null, title: "All runs", kind: "home" };
}

function normalizeTabs(raw) {
  if (!Array.isArray(raw) || raw.length === 0) return [tabForRoute("welcome")];
  const out = [];
  const seen = new Set();
  for (const t of raw) {
    if (!t || !t.key || seen.has(t.key)) continue;
    if (!["welcome", "new", "live", "results"].includes(t.name)) continue;
    seen.add(t.key);
    out.push({
      key: t.key,
      name: t.name,
      runId: t.runId || null,
      title: t.title || "Untitled",
      kind: t.kind || t.name,
    });
  }
  return out.length ? out.slice(-12) : [tabForRoute("welcome")];
}

function loadTabs() {
  try {
    return normalizeTabs(JSON.parse(localStorage.getItem(WORKSPACE_TABS_KEY) || "[]"));
  } catch {
    return [tabForRoute("welcome")];
  }
}

const App = () => {
  const [hash, setHash] = useStateApp(parseHash);
  useEffectApp(() => {
    const onHash = () => setHash(parseHash());
    window.addEventListener("hashchange", onHash);
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  const [tweaks, setTweaks] = window.useTweaks
    ? window.useTweaks((() => {
        const d = { ...TWEAK_DEFAULTS };
        const p = hash.params || {};
        if (p.theme === "light") d.theme = "light";
        if (p.theme === "mono") { d.theme = "dark"; d.accent = "amber"; d._mono = true; }
        if (p.map) d.mapStyle = p.map;
        if (p.accent) d.accent = p.accent;
        return d;
      })())
    : useStateApp(() => ({ ...TWEAK_DEFAULTS }));
  const [railCollapsed, setRailCollapsed] = useStateApp(() => {
    try { return localStorage.getItem("parallel_scraper_rail_collapsed") === "1"; }
    catch { return false; }
  });

  // apply theme/density to root
  useEffectApp(() => {
    document.documentElement.setAttribute("data-theme", tweaks.theme || "dark");
    document.documentElement.setAttribute("data-density", tweaks.density || "comfortable");
    document.documentElement.setAttribute("data-mapstyle", tweaks.mapStyle || "dark");
    const a = ACCENTS[tweaks.accent] || ACCENTS.lime;
    document.documentElement.style.setProperty("--accent", a.c);
    document.documentElement.style.setProperty("--accent-fg", a.fg);
  }, [tweaks.theme, tweaks.density, tweaks.mapStyle, tweaks.accent]);

  // global keyboard shortcuts
  useEffectApp(() => {
    const onKey = (e) => {
      const tag = (e.target && e.target.tagName) || "";
      if (tag === "INPUT" || tag === "TEXTAREA" || e.target?.isContentEditable) return;
      if (e.key === "n" || e.key === "N") { e.preventDefault(); setRoute("new"); }
      if (e.key === "Escape" && hash.name === "new") { e.preventDefault(); setRoute("welcome"); }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [hash.name]);

  const setRoute = (name, runId) => {
    location.hash = buildHash(name, runId);
  };

  const { runs, refresh: refreshRuns } = window.useRuns(10000);
  const route = hash.name === "all" ? "welcome" : hash.name;
  const [tabs, setTabs] = useStateApp(loadTabs);
  const activeTabKey = tabKey(route, hash.runId || "");

  const openTab = (name, runId) => {
    const nextTab = tabForRoute(name, runId, runs);
    setTabs(prev => {
      const existing = prev.filter(t => t.key !== nextTab.key);
      return normalizeTabs([...existing, nextTab]);
    });
    setRoute(nextTab.name, nextTab.runId);
  };

  const openRun = (run, view) => {
    if (!run) return;
    openTab(view || (run.status === "running" ? "live" : "results"), run.id);
  };

  const closeTab = (key, ev) => {
    if (ev) ev.stopPropagation();
    setTabs(prev => {
      const idx = prev.findIndex(t => t.key === key);
      const next = prev.filter(t => t.key !== key);
      if (key === activeTabKey) {
        const fallback = next[Math.max(0, Math.min(idx, next.length - 1))] || tabForRoute("welcome");
        setTimeout(() => setRoute(fallback.name, fallback.runId), 0);
      }
      return normalizeTabs(next);
    });
  };

  // Resolve "current run" — explicit runId in hash, else most-recent matching status.
  const currentRun = useMemoApp(() => {
    if (hash.runId) {
      const r = runs.find(x => x.id === hash.runId);
      return r || (hash.runId ? { id: hash.runId, name: hash.runId, area: "—", status: "running",
                                  started: "—", queries: 0, queries_arr: [], cells: 0, found: 0,
                                  scraped: 0, eta: "—", cost: 0, raw: {} } : null);
    }
    if (hash.name === "live") return runs.find(r => r.status === "running") || runs[0] || null;
    if (hash.name === "results") return runs.find(r => r.status === "done") || runs[0] || null;
    return null;
  }, [runs, hash.name, hash.runId]);

  useEffectApp(() => {
    const nextTab = tabForRoute(hash.name, hash.runId, runs);
    setTabs(prev => {
      const existing = prev.filter(t => t.key !== nextTab.key);
      return normalizeTabs([...existing, nextTab]);
    });
  }, [hash.name, hash.runId]);

  useEffectApp(() => {
    try { localStorage.setItem(WORKSPACE_TABS_KEY, JSON.stringify(tabs)); } catch {}
  }, [tabs]);

  useEffectApp(() => {
    try { localStorage.setItem("parallel_scraper_rail_collapsed", railCollapsed ? "1" : "0"); } catch {}
  }, [railCollapsed]);

  const displayTabs = useMemoApp(() => tabs.map(t => {
    const run = t.runId ? runs.find(r => r.id === t.runId) : null;
    const status = run ? run.status : (t.kind === "live" ? "running" : t.kind === "results" ? "done" : "");
    const title = run
      ? (t.name === "live" ? `Live: ${run.area || run.name}` : `Results: ${run.area || run.name}`)
      : t.title;
    const sub = run ? `${run.scraped || 0}/${run.found || 0}` : "";
    return { ...t, title, status, sub };
  }), [tabs, runs]);

  const setTweak = (k, v) => setTweaks({ [k]: v });

  return (
    <div className="app" data-screen-label={`01 ${route}`} data-rail={railCollapsed ? "collapsed" : "expanded"}>
      <window.Sidebar
        route={route}
        setRoute={setRoute}
        openRun={openRun}
        runs={runs}
        currentRunId={currentRun ? currentRun.id : null}
        onRefresh={refreshRuns}
        collapsed={railCollapsed}
        onToggleCollapsed={() => setRailCollapsed(v => !v)}
      />
      <main className="main">
        <header className="topbar">
          <div className="crumb">
            {route === "welcome" && <><b>parallel-scraper</b><span className="sep">/</span>home</>}
            {route === "new" && <><span>parallel-scraper</span><span className="sep">/</span><b>new run</b></>}
            {route === "live" && <><span>parallel-scraper</span><span className="sep">/</span><span>runs</span><span className="sep">/</span><b className="num">{currentRun ? currentRun.id : "—"}</b><span className="chip running dot" style={{marginLeft:4}}>live</span></>}
            {route === "results" && <><span>parallel-scraper</span><span className="sep">/</span><span>runs</span><span className="sep">/</span><b className="num">{currentRun ? currentRun.id : "—"}</b><span className={`chip ${currentRun && currentRun.status === "failed" ? "failed" : "done"} dot`} style={{marginLeft:4}}>{currentRun ? currentRun.status : "done"}</span></>}
          </div>
          <span className="spacer"/>
          {route === "live" && <span className="num faint" style={{fontSize:11}}>auto-refresh · 2s</span>}
          <button className="btn ghost icon" title="settings"><window.Icons.Settings size={13}/></button>
          <button className="btn ghost" title="open output dir" onClick={() => alert("Open " + (currentRun ? `outputs/${currentRun.id}/` : "outputs/") + " in your file manager.")}><window.Icons.Folder size={11}/> runs/</button>
        </header>

        <nav className="desktop-tabs" aria-label="open workspace tabs">
          {displayTabs.map(t => (
            <button
              key={t.key}
              className={`desk-tab ${t.key === activeTabKey ? "active" : ""} ${t.status || ""}`}
              title={t.runId || t.title}
              onClick={() => setRoute(t.name, t.runId)}
            >
              <span className="desk-tab-dot" />
              <span className="desk-tab-title">{t.title}</span>
              {t.sub && <span className="desk-tab-sub num">{t.sub}</span>}
              <span
                className="desk-tab-close"
                role="button"
                tabIndex={0}
                title="close tab"
                onClick={(ev) => closeTab(t.key, ev)}
                onKeyDown={(ev) => { if (ev.key === "Enter" || ev.key === " ") closeTab(t.key, ev); }}
              >
                <window.Icons.X size={9}/>
              </span>
            </button>
          ))}
        </nav>

        {route === "welcome" && <window.Welcome setRoute={setRoute} openRun={openRun} runs={runs}/>}
        {route === "new"     && <window.NewRun setRoute={setRoute} mapStyle={tweaks.mapStyle}/>}
        {route === "live"    && <window.LiveRun run={currentRun} mapStyle={tweaks.mapStyle} setRoute={setRoute}/>}
        {route === "results" && <window.Results run={currentRun} mapStyle={tweaks.mapStyle}/>}
      </main>

      {window.TweaksPanel && hash.params.tweaks === "1" && (
        <window.TweaksPanel title="Tweaks">
          <window.TweakSection label="appearance">
            <window.TweakRadio   label="theme"     value={tweaks.theme}    options={[{value:"dark",label:"Dark"},{value:"light",label:"Light"}]} onChange={v=>setTweak("theme", v)}/>
            <window.TweakSelect  label="accent"    value={tweaks.accent}   options={Object.keys(ACCENTS).map(k=>({value:k,label:k}))} onChange={v=>setTweak("accent", v)}/>
            <window.TweakRadio   label="density"   value={tweaks.density}  options={[{value:"comfortable",label:"Comfy"},{value:"compact",label:"Compact"}]} onChange={v=>setTweak("density", v)}/>
            <window.TweakSelect  label="map style" value={tweaks.mapStyle} options={[{value:"dark",label:"Dark"},{value:"light",label:"Light"},{value:"satellite",label:"Satellite"}]} onChange={v=>setTweak("mapStyle", v)}/>
          </window.TweakSection>
          <window.TweakSection label="navigate">
            <div style={{display:"grid", gridTemplateColumns:"1fr 1fr", gap:6}}>
              <window.TweakButton label="Welcome" onClick={()=>setRoute("welcome")}/>
              <window.TweakButton label="New run" onClick={()=>setRoute("new")}/>
              <window.TweakButton label="Live run" onClick={()=>setRoute("live")}/>
              <window.TweakButton label="Results" onClick={()=>setRoute("results")}/>
            </div>
          </window.TweakSection>
        </window.TweaksPanel>
      )}
    </div>
  );
};

ReactDOM.createRoot(document.getElementById("root")).render(<App/>);
