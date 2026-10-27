import { useEffect, useMemo, useRef, useState } from 'react';
import { architectureStatus, architectureViews, architectureViewIds, buildRecordedLiveGraph, validateArchitectureGraph } from './architectureGraphModern.js';

const WIDTH = 1200;
const HEIGHT = 680;

function statusLabel(status) {
  return architectureStatus[status]?.label ?? 'Unclassified';
}

function GraphNode({ item, selected, onSelect }) {
  return <g className={`architecture-node architecture-node--${item.status}${selected ? ' is-selected' : ''}`}
    transform={`translate(${item.x} ${item.y})`} role="button" tabIndex="0" aria-label={`${item.label}: ${statusLabel(item.status)}`}
    onClick={() => onSelect(item)} onKeyDown={(event) => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); onSelect(item); } }}>
    <rect x="-78" y="-34" width="156" height="68" rx="13" />
    <circle className="architecture-node__dot" cx="-61" cy="-16" r="5" />
    <text className="architecture-node__label" x="-49" y="-10">{item.label.length > 22 ? `${item.label.slice(0, 21)}…` : item.label}</text>
    <text className="architecture-node__status" x="-49" y="12">{statusLabel(item.status).replace('Previously deployed; not live-queried here', 'Previously deployed').replace('Unavailable in this local context', 'Unavailable')}</text>
  </g>;
}

function ArchitectureExplorer({ api, identity }) {
  const [viewId, setViewId] = useState('agent');
  const [zoom, setZoom] = useState(0.82);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const [selected, setSelected] = useState(null);
  const [playing, setPlaying] = useState(true);
  const [replayKey, setReplayKey] = useState(0);
  const [activeEdge, setActiveEdge] = useState(0);
  const [live, setLive] = useState({ phase: 'idle', investigations: [], events: [], error: '' });
  const drag = useRef(null);

  useEffect(() => {
    if (!playing) return undefined;
    const timer = globalThis.setInterval(() => setActiveEdge((value) => value + 1), 1000);
    return () => globalThis.clearInterval(timer);
  }, [playing, replayKey]);

  useEffect(() => {
    if (viewId !== 'live' || !api?.diagnosticInvestigations || !api?.diagnosticEvents) return undefined;
    let mounted = true;
    setLive((value) => ({ ...value, phase: 'loading', error: '' }));
    Promise.all([
      api.diagnosticInvestigations({ limit: 25 }),
      Promise.all(['workflow', 'model', 'tools'].map((category) => api.diagnosticEvents(category, { limit: 50 }))),
    ]).then(([investigations, pages]) => {
      if (!mounted) return;
      setLive({ phase: 'ready', investigations: investigations.items, events: pages.flatMap((page) => page.events), error: '' });
    }).catch(() => { if (mounted) setLive({ phase: 'error', investigations: [], events: [], error: 'Recorded workflow data is unavailable.' }); });
    return () => { mounted = false; };
  }, [api, viewId]);

  const graph = useMemo(() => viewId === 'live'
    ? buildRecordedLiveGraph(live.investigations, live.events)
    : architectureViews[viewId], [live.events, live.investigations, viewId]);
  const valid = validateArchitectureGraph(graph);
  const nodes = useMemo(() => new Map(graph.nodes.map((item) => [item.id, item])), [graph]);

  if (identity?.role !== 'ADMIN') {
    return <section className="view-placeholder" aria-labelledby="architecture-access-title"><p className="eyebrow">Architecture</p><h2 id="architecture-access-title">Administrator access required.</h2><p>This source-backed architecture explorer is limited to administrators.</p></section>;
  }

  function resetViewport() { setZoom(0.82); setPan({ x: 0, y: 0 }); }
  function startDrag(event) {
    if (event.target.closest?.('.architecture-node')) return;
    drag.current = { x: event.clientX, y: event.clientY, pan };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }
  function moveDrag(event) {
    if (!drag.current) return;
    setPan({ x: drag.current.pan.x + (event.clientX - drag.current.x), y: drag.current.pan.y + (event.clientY - drag.current.y) });
  }
  function endDrag() { drag.current = null; }
  function replay() { setActiveEdge(0); setPlaying(true); setReplayKey((value) => value + 1); setSelected(null); }

  return <section className="architecture-workspace" aria-labelledby="architecture-title">
    <div className="workspace-section-heading"><div><p className="eyebrow">ADMIN-only · source-backed map</p><h2 id="architecture-title">How NovaMind Works</h2><p>Explore the recorded architecture and workflow boundaries. Visual replay is read-only and never starts a run.</p></div><span className="architecture-safety">No AWS calls · no execution</span></div>
    <div className="architecture-tabs" role="tablist" aria-label="Architecture views">
      {architectureViewIds.map((id) => <button key={id} type="button" role="tab" aria-selected={viewId === id} className={viewId === id ? 'is-active' : ''} onClick={() => { setViewId(id); setSelected(null); resetViewport(); }}>{architectureViews[id].title}</button>)}
    </div>
    <div className="architecture-toolbar" role="toolbar" aria-label="Graph controls">
      <button type="button" className="button-secondary" onClick={() => setPlaying((value) => !value)}>{playing ? 'Pause' : 'Play'} flow</button>
      <button type="button" className="button-secondary" onClick={replay}>Replay</button>
      <button type="button" className="button-secondary" onClick={() => setZoom((value) => Math.min(1.8, value + 0.1))} aria-label="Zoom in">+</button>
      <button type="button" className="button-secondary" onClick={() => setZoom((value) => Math.max(0.5, value - 0.1))} aria-label="Zoom out">−</button>
      <button type="button" className="button-secondary" onClick={resetViewport}>Fit to screen</button>
      <span className="architecture-toolbar__hint">Drag canvas to pan · click a node for evidence</span>
    </div>
    <div className="architecture-layout">
      <div className="architecture-canvas" onPointerDown={startDrag} onPointerMove={moveDrag} onPointerUp={endDrag} onPointerCancel={endDrag}>
        <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label={`${graph.title} connected-node graph`}>
          <defs><marker id="architecture-arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" /></marker></defs>
          <g transform={`translate(${pan.x} ${pan.y}) scale(${zoom})`}>
            {graph.edges.map((item, index) => { const from = nodes.get(item.from); const to = nodes.get(item.to); return <g key={`${item.from}-${item.to}`} className={`architecture-edge${playing && index === activeEdge % graph.edges.length ? ' is-active' : ''}`}>
              <line x1={from.x} y1={from.y} x2={to.x} y2={to.y} markerEnd="url(#architecture-arrow)" />
              <text x={(from.x + to.x) / 2} y={(from.y + to.y) / 2 - 8}>{item.label}</text>
            </g>; })}
            {graph.nodes.map((item) => <GraphNode key={item.id} item={item} selected={selected?.id === item.id} onSelect={setSelected} />)}
          </g>
        </svg>
      </div>
      <aside className="architecture-details" aria-live="polite">
        <p className="eyebrow">{graph.title}</p><h3>{graph.subtitle}</h3>
        {viewId === 'live' && <div className="architecture-live-state"><strong>{live.phase === 'loading' ? 'Loading recorded data…' : live.phase === 'error' ? 'Recorded data unavailable' : `${live.investigations.length} investigation record(s)`}</strong><span>{live.error || 'Local diagnostic history only. No workflow is replayed.'}</span></div>}
        {selected ? <><span className={`architecture-status architecture-status--${selected.status}`}>{statusLabel(selected.status)}</span><h3>{selected.label}</h3><p>{selected.detail}</p><p className="architecture-evidence"><strong>Evidence:</strong> {selected.evidence}</p></> : <p className="architecture-empty">Select a node to inspect its role, evidence status and boundary.</p>}
        <div className="architecture-legend" aria-label="Evidence legend">{Object.entries(architectureStatus).map(([key, value]) => <span key={key}><i className={`architecture-legend__dot architecture-legend__dot--${key}`} />{value.label}</span>)}</div>
        {!valid && <p className="run-notice">This graph could not be validated locally.</p>}
      </aside>
    </div>
  </section>;
}

export default ArchitectureExplorer;
