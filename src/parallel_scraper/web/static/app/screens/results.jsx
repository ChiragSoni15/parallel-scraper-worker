// Results split view — table + map + detail panel
const { useState: useStateRes, useMemo: useMemoRes, useEffect: useEffectRes } = React;

const Results = ({ run, mapStyle }) => {
  const I = window.Icons;
  const runId = run && run.id;
  const [filter, setFilter] = useStateRes("");
  const [selected, setSelected] = useStateRes(null);
  // Defer the (potentially slow) /leads fetch until the user explicitly asks.
  // For a 175K-lead Mumbai-scale run on OneDrive that initial call is 1-3s,
  // and the table is only useful once they want to drill in.
  const [outletsEnabled, setOutletsEnabled] = useStateRes(false);
  const { leads, total, loaded, loading } = window.useRunLeads(runId, filter, 0, 500, outletsEnabled);
  const boundary = window.useRunBoundary(runId);

  // Auto-select first lead when leads load (or when leads change and current selection vanishes).
  useEffectRes(() => {
    if (!leads.length) { setSelected(null); return; }
    if (!leads.some(l => l.id === selected)) setSelected(leads[0].id);
  }, [leads, selected]);

  const sel = useMemoRes(() => leads.find(l => l.id === selected) || null, [leads, selected]);
  const pins = useMemoRes(() => leads
    .filter(l => Number.isFinite(l.lat) && Number.isFinite(l.lng))
    .map(l => ({ id: l.id, lat: l.lat, lng: l.lng })),
    [leads]);
  const mapLabel = run?.name || runId;

  function exportCsv() {
    if (!runId) return;
    const url = `/api/runs/${encodeURIComponent(runId)}/leads.csv${filter ? `?q=${encodeURIComponent(filter)}` : ""}`;
    window.open(url, "_blank");
  }

  if (!runId) {
    return (
      <div className="welcome-grid">
        <section className="welcome-hero">
          <h1 className="welcome-h1">no run selected</h1>
          <p className="welcome-sub">pick a completed run from the sidebar to view its results.</p>
        </section>
      </div>
    );
  }

  return (
    <div className="res-grid">
      {/* table */}
      <section className="res-table-wrap">
        <div className="res-toolbar">
          {outletsEnabled ? (
            <>
              <span style={{display:"flex", alignItems:"center", gap:6}}>
                <I.Search size={12} className="muted"/>
                <input className="input" style={{width:200, height:28}}
                       placeholder="filter places…"
                       value={filter} onChange={e=>setFilter(e.target.value)}/>
              </span>
              <span style={{flex:1}}/>
              <span className="num faint" style={{fontSize:11}}>
                {loading ? "loading…" : loaded ? `${leads.length} of ${total}` : "—"}
              </span>
              <button className="btn sm" onClick={exportCsv}><I.Download size={11}/> export csv</button>
            </>
          ) : (
            <>
              <span style={{display:"flex", alignItems:"center", gap:8, fontSize:12.5, fontWeight:500}}>
                <span className={`chip dot ${run?.status || "done"}`}>{run?.status || "done"}</span>
                <span>{run?.name || runId}</span>
              </span>
              <span style={{flex:1}}/>
              <span className="num faint" style={{fontSize:11}}>
                {(run?.found || 0).toLocaleString()} places found
              </span>
            </>
          )}
        </div>
        <div className="res-table-scroll">
          {!outletsEnabled ? (
            <div className="muted" style={{padding:"32px 24px", textAlign:"center", display:"flex", flexDirection:"column", alignItems:"center", gap:14}}>
              <div style={{fontSize:13, lineHeight:1.6, maxWidth:380}}>
                Loading the full outlet list parses every scraped row + every Phase 1 discovery on the server.
                For this run that's <span className="num">{(run?.found || 0).toLocaleString()}</span> rows
                and can take a few seconds on first hit.
              </div>
              <button className="btn primary" onClick={()=>setOutletsEnabled(true)}>
                <I.Download size={12}/> load outlets
              </button>
              <div className="dim" style={{fontSize:11}}>
                tip: you can also export the CSV directly without loading the table —
                <button className="btn sm ghost" style={{marginLeft:6, fontSize:11}} onClick={exportCsv}>
                  <I.Download size={10}/> export csv
                </button>
              </div>
            </div>
          ) : loading || !loaded ? (
            <div className="muted" style={{padding:"24px", textAlign:"center"}}>loading leads…</div>
          ) : leads.length === 0 ? (
            <div className="muted" style={{padding:"24px", textAlign:"center"}}>
              {total === 0 ? "no leads scraped yet for this run." : "no matches for filter."}
            </div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th></th>
                  <th>name</th>
                  <th>category</th>
                  <th style={{textAlign:"right"}}>rating</th>
                  <th>address</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {leads.map(l => (
                  <tr key={l.id} aria-selected={l.id === selected} onClick={()=>setSelected(l.id)}>
                    <td><span className={`chip dot ${l.scraped ? "done" : "warn"}`} style={{padding:"1px 6px"}}>{l.scraped ? "scraped" : "queued"}</span></td>
                    <td>
                      <div style={{fontWeight:500}}>{l.name || "—"}</div>
                      <div className="dim num" style={{fontSize:10.5}}>{l.id}</div>
                    </td>
                    <td className="dim">{l.cat || "—"}</td>
                    <td className="num" style={{textAlign:"right"}}>
                      {l.rating ? <>{l.rating} <span className="dim" style={{fontSize:10.5}}>({l.rcount || 0})</span></> : "—"}
                    </td>
                    <td className="dim" style={{maxWidth:200, overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap"}}>{l.addr || "—"}</td>
                    <td><I.ChevronRight size={11} className="faint"/></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      {/* map */}
      <section className="res-map">
        <window.MapView
          mapStyle={mapStyle}
          boundary={boundary}
          pins={pins}
          selectedPinId={selected}
          onPinClick={setSelected}
          fitTo={boundary ? "boundary" : "pins"}
          overlayLabel={
            <div className="map-overlay-inline">
              <span className={`chip ${run?.status || "done"} dot map-overlay-chip`} title={mapLabel}>
                <span className="map-overlay-text">{mapLabel}</span>
              </span>
              <span className="map-overlay-count num faint">{pins.length} pins</span>
            </div>
          }
        />
      </section>

      {/* detail */}
      <aside className="res-detail">
        {sel ? (
          <>
            <div className="res-detail-head">
              <div style={{display:"flex", alignItems:"center", gap:8, marginBottom:6}}>
                <span className={`chip dot ${sel.scraped ? "done" : "warn"}`}>{sel.scraped ? "scraped" : "pending"}</span>
                {sel.cat && <span className="chip">{sel.cat}</span>}
              </div>
              <h2 style={{margin:0, fontSize:16, fontWeight:600, letterSpacing:"-0.005em"}}>{sel.name || "—"}</h2>
              <div className="muted num" style={{fontSize:11, marginTop:4}}>place_id · {sel.id}</div>
            </div>
            <div className="res-detail-body">
              {sel.image_url ? (
                <img src={sel.image_url} alt={sel.name} style={{width:"100%", aspectRatio:"4/3", objectFit:"cover", borderRadius:"var(--r-md)"}}/>
              ) : (
                <div className="imgph">PHOTO PLACEHOLDER · 4:3</div>
              )}

              {sel.rating && (
                <div style={{display:"flex", alignItems:"center", gap:8}}>
                  <I.Star size={13} style={{color:"var(--st-warn)"}}/>
                  <span style={{fontSize:14, fontWeight:600}}>{sel.rating}</span>
                  <span className="muted num" style={{fontSize:11}}>{sel.rcount || 0} reviews</span>
                </div>
              )}

              <dl className="kv">
                {sel.addr && <><dt>address</dt><dd>{sel.addr}</dd></>}
                {sel.phone && <><dt>phone</dt><dd className="mono">{sel.phone}</dd></>}
                {sel.website && <><dt>website</dt><dd className="mono">{sel.website}</dd></>}
                {Number.isFinite(sel.lat) && Number.isFinite(sel.lng) && (
                  <><dt>coords</dt><dd className="mono">{sel.lat.toFixed(4)}°, {sel.lng.toFixed(4)}°</dd></>
                )}
                {sel.found_via && <><dt>found via</dt><dd className="mono">q={sel.found_via}</dd></>}
              </dl>

              <hr className="hr"/>

              <div style={{display:"flex", flexDirection:"column", gap:6}}>
                <div className="field-label">actions</div>
                <a className="btn" target="_blank" rel="noreferrer"
                   href={`https://www.google.com/maps/place/?q=place_id:${encodeURIComponent(sel.id)}`}>
                  <I.External size={11}/> open in Google Maps
                </a>
                {sel.website && sel.website !== "—" && (
                  <a className="btn ghost" href={sel.website.startsWith("http") ? sel.website : `https://${sel.website}`} target="_blank" rel="noreferrer">
                    <I.External size={11}/> visit website
                  </a>
                )}
              </div>

              {leads.length > 1 && (
                <div className="panel" style={{padding:"10px 12px"}}>
                  <div className="field-label" style={{marginBottom:6}}>others in this run</div>
                  {leads.filter(l => l.id !== sel.id).slice(0, 5).map(l => (
                    <button key={l.id} className="rail-item" style={{padding:"4px 6px"}} onClick={()=>setSelected(l.id)}>
                      <span style={{width:6, height:6, borderRadius:"50%", background:"var(--accent)"}}/>
                      <span style={{flex:1, textAlign:"left", overflow:"hidden", textOverflow:"ellipsis", whiteSpace:"nowrap", fontSize:12}}>{l.name || l.id}</span>
                      {l.rating && <span className="num faint" style={{fontSize:10.5}}>{l.rating}★</span>}
                    </button>
                  ))}
                </div>
              )}
            </div>
          </>
        ) : (
          <div className="muted" style={{padding:"24px", textAlign:"center", fontSize:12}}>
            select a row to see details.
          </div>
        )}
      </aside>
    </div>
  );
};

window.Results = Results;
