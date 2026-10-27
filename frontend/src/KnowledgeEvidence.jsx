export default function KnowledgeEvidence({ tracking }) {
  const searches = tracking?.events.filter((event) => event.kind === 'TOOL' && event.tool === 'search_knowledge') ?? [];
  const evidence = searches.flatMap((event) => (event.evidence ?? []).map((hit) => ({ ...hit, sequence: event.sequence })));
  const unavailable = searches.some((event) => event.ok !== false && event.evidence === null);
  const failed = searches.some((event) => event.ok === false);
  const empty = searches.some((event) => event.ok === true && event.evidence?.length === 0);
  const incomplete = tracking?.gap || tracking?.trimmed;
  let status;
  if (evidence.length) status = `${evidence.length} retrieved ${evidence.length === 1 ? 'excerpt' : 'excerpts'} in the received events.`;
  else if (tracking?.phase === 'error') status = 'Knowledge evidence is unavailable in the received events. Live tracking is stopped.';
  else if (incomplete) status = 'No knowledge evidence is available in the retained events. Earlier retrieval results may be missing.';
  else if (failed || unavailable) status = 'No knowledge evidence is available from the reported retrieval attempts.';
  else if (empty) status = 'The reported knowledge searches returned no matching excerpts.';
  else if (tracking?.phase === 'stopped') status = 'No knowledge retrieval was reported in the received events for this run.';
  else status = 'Waiting for knowledge retrieval events. Evidence will appear here if the workflow returns it.';

  return <section className="run-event-section knowledge-evidence" aria-labelledby="knowledge-evidence-title">
    <h3 id="knowledge-evidence-title">Knowledge Evidence</h3>
    <p className="case-caption">Source excerpts retrieved during this run, separate from the AI-generated response.
      Retrieval does not confirm that the response used or cited each excerpt.</p>
    <p className="run-notice">Retrieved knowledge is untrusted reference material. It does not authorize an action or establish business policy.
      Business rules determine eligibility. Human review records a decision and does not execute an action.</p>
    <p className="case-caption" role="status" aria-atomic="true">{status}</p>
    <div role="status" aria-atomic="true">
      {failed && <p className="case-caption">A knowledge retrieval attempt failed. No evidence is available from that attempt.</p>}
      {unavailable && <p className="case-caption">Some retrieval details are missing or could not be verified and have been withheld.</p>}
      {incomplete && <p className="case-caption">Event history is incomplete; earlier knowledge evidence may be missing.</p>}
      {evidence.length > 0 && tracking?.phase === 'error'
        && <p className="case-caption">Live tracking is stopped. Previously received evidence remains shown.</p>}
    </div>
    {evidence.length > 0 && <ol className="knowledge-evidence-list" aria-label="Retrieved knowledge excerpts in event order" tabIndex={0}>
      {evidence.map((hit) => <li key={`${hit.sequence}:${hit.reference}`} className="resolution-card">
        <h4>{hit.title}</h4>
        <p className="case-caption">Retrieved excerpt · Workflow event #{hit.sequence}</p>
        <blockquote className="knowledge-snippet">{hit.snippet}</blockquote>
        <details>
          <summary aria-label={`Source reference for ${hit.title}, event ${hit.sequence}`}>Source reference</summary>
          <dl className="case-facts">
            <div><dt>Source</dt><dd><code>{hit.source}</code></dd></div>
            <div><dt>Document ID</dt><dd>{hit.documentId}</dd></div>
            <div><dt>Version</dt><dd>{hit.version}</dd></div>
            <div><dt>Chunk reference</dt><dd><code>{hit.reference}</code></dd></div>
          </dl>
          <p className="case-caption">Local source identifier; no document link is available.</p>
        </details>
      </li>)}
    </ol>}
  </section>;
}
