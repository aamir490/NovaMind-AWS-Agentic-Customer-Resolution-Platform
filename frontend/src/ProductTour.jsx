import { useEffect, useState } from 'react';
import { clampTourStep, productTourServices, productTourSteps } from './productTour.js';

function TourIcon({ id }) {
  const paths = {
    complaint: <><path d="M5 5h14v10H9l-4 4z" /><path d="M8 9h8M8 12h5" /></>,
    investigation: <><path d="M12 3v4M12 17v4M3 12h4M17 12h4M5.6 5.6l2.8 2.8M15.6 15.6l2.8 2.8M18.4 5.6l-2.8 2.8M8.4 15.6l-2.8 2.8" /><circle cx="12" cy="12" r="4" /></>,
    review: <><circle cx="12" cy="8" r="3" /><path d="M5 20c1-4 3-6 7-6s6 2 7 6M16 4l2 2 3-3" /></>,
    response: <><path d="M5 5h14v10H9l-4 4z" /><path d="m9 10 2 2 4-4" /></>,
  };
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[id]}</svg>;
}

export default function ProductTour() {
  const [activeStep, setActiveStep] = useState(0);
  const [playing, setPlaying] = useState(false);
  const step = productTourSteps[activeStep];

  useEffect(() => {
    if (!playing) return undefined;
    const timer = globalThis.setInterval(() => setActiveStep((value) => {
      if (value >= productTourSteps.length - 1) { setPlaying(false); return value; }
      return value + 1;
    }), 3200);
    return () => globalThis.clearInterval(timer);
  }, [playing]);

  function selectStep(index) { setActiveStep(clampTourStep(index)); }
  function replay() { setActiveStep(0); setPlaying(true); }

  return <section className="product-tour" aria-labelledby="product-tour-title">
    <div className="product-tour__heading"><div><p className="eyebrow">Interactive product tour</p><h2 id="product-tour-title">From customer voice to human-approved resolution.</h2><p>Explore a synthetic example of how NovaMind connects evidence, AI assistance and human judgment.</p></div><span className="product-tour__badge">Read-only demo · no data submitted</span></div>
    <div className="product-tour__steps" role="tablist" aria-label="Product tour steps">
      {productTourSteps.map((item, index) => <button key={item.id} type="button" role="tab" aria-selected={index === activeStep} aria-controls={`tour-panel-${item.id}`} className={`product-tour__step product-tour__step--${item.tone}${index === activeStep ? ' is-active' : ''}`} onClick={() => selectStep(index)}><span className="product-tour__step-number">{item.number}</span><span>{item.label}</span></button>)}
    </div>
    <div className="product-tour__progress" aria-hidden="true"><span style={{ width: `${((activeStep + 1) / productTourSteps.length) * 100}%` }} /></div>
    <div className="product-tour__panel" id={`tour-panel-${step.id}`} role="tabpanel" aria-live="polite" tabIndex="0">
      <div className={`product-tour__icon product-tour__icon--${step.tone}`}><TourIcon id={step.id} /></div><div className="product-tour__copy"><p className="eyebrow">Step {step.number} of {String(productTourSteps.length).padStart(2, '0')}</p><h3>{step.title}</h3><p>{step.description}</p><p className="product-tour__outcome"><strong>What happens:</strong> {step.outcome}</p></div><div className="product-tour__preview"><span>{step.previewLabel}</span><p>{step.preview}</p><i aria-hidden="true"><b /><b /><b /></i></div>
    </div>
    <div className="product-tour__controls"><button type="button" className="button-secondary" onClick={() => selectStep(activeStep - 1)} disabled={activeStep === 0}>← Previous</button><button type="button" onClick={() => setPlaying((value) => !value)}>{playing ? 'Pause tour' : 'Play tour'} <span aria-hidden="true">{playing ? 'Ⅱ' : '▶'}</span></button><button type="button" className="button-secondary" onClick={() => selectStep(activeStep + 1)} disabled={activeStep === productTourSteps.length - 1}>Next →</button><button type="button" className="product-tour__replay" onClick={replay}>Replay</button></div>
    <div className="product-tour__architecture" aria-labelledby="tour-architecture-title"><div><p className="eyebrow">Conceptual AWS architecture</p><h3 id="tour-architecture-title">A secure path from edge to review.</h3><p>Service names reflect NovaMind’s deployment shape. This public tour does not query AWS or represent live resource state.</p></div><div className="product-tour__services">{productTourServices.map((service, index) => <div key={service.name} className="product-tour__service"><span>{String(index + 1).padStart(2, '0')}</span><strong>{service.name}</strong><small>{service.detail}</small></div>)}</div></div>
  </section>;
}
