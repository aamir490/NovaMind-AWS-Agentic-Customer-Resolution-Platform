import { StrictMode, useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './styles.css';
import logoUrl from './assets/novamind-logo.png';
import Dashboard from './Dashboard.jsx';
import CaseReview from './CaseReview.jsx';
import AIWorkspace from './AIWorkspace.jsx';
import ReviewQueue from './ReviewQueue.jsx';
import AuditWorkspace from './AuditWorkspace.jsx';
import DiagnosticsWorkspace from './DiagnosticsWorkspace.jsx';
import ArchitectureExplorer from './ArchitectureExplorer.jsx';
import AnalyticsWorkspace from './AnalyticsWorkspace.jsx';
import CustomerPortal from './CustomerPortal.jsx';
import { CustomerProvisioning } from './CustomerProvisioning.jsx';
import { createApi } from './api.js';
import {
  beginCognitoLogin, finishCognitoLogin, hasCognitoCallback,
  clearCognitoPortalIntent, readCognitoConfiguration, readCognitoPortalIntent,
  rememberCognitoPortal, signOutOfCognito,
} from './cognitoAuth.js';
import { applyTheme, readThemePreference, resolveTheme, storeThemePreference } from './theme.js';

// Internal staff views — never shown to CUSTOMER role.
const staffViews = [
  { id: 'dashboard', label: 'Dashboard', title: 'Dashboard',
    description: 'Operations overview \u2014 cases, reviews, metrics and recent activity.' ,
    icon: 'M4 4h6v6H4zM14 4h6v6h-6zM4 14h6v6H4zM14 14h6v6h-6z' },
  { id: 'cases', label: 'Cases', title: 'Case review',
    description: 'Customer context, policy, and eligibility. One place to review the details.',
    icon: 'M4 5h16v15H4zM9 5V3h6v2M8 10h8M8 14h5' },
  { id: 'ai-workspace', label: 'AI Workspace', title: 'AI Workspace',
    description: 'Submit a case-bound request and follow its status and workflow events.',
    icon: 'M5 4h14v12H9l-4 4zM8 8h8M8 12h5' },
  { id: 'reviews', label: 'Reviews', title: 'Reviews',
    description: 'Assess pending proposals and record an explicit human review decision.',
    icon: 'M9 4H5v17h14V4h-4M9 3h6v4H9zM8 13l3 3 5-6' },
  { id: 'audit', label: 'Audit', title: 'Audit',
    description: 'Inspect safe workflow and human review metadata for an existing run.',
    icon: 'M5 3h10l4 4v14H5zM14 3v5h5M9 12h6M9 16h6' },
  { id: 'diagnostics', label: 'Diagnostics', title: 'Diagnostics',
    description: 'Inspect recorded operational metrics and recent trace metadata.',
    icon: 'M3 12h4l3-7 4 14 3-7h4' },
  { id: 'architecture', label: 'How NovaMind Works', title: 'How NovaMind Works',
    description: 'Explore the source-backed architecture and workflow boundaries.',
    icon: 'M5 5h5v5H5zM14 5h5v5h-5zM5 14h5v5H5zM14 14h5v5h-5zM10 7.5h4M7.5 10v4M16.5 10v4M10 16.5h4' },
  { id: 'analytics', label: 'Analytics', title: 'AI Resolution Analytics',
    description: 'Review durable aggregate investigation and resolution measurements.',
    icon: 'M4 19V5M4 19h16M8 16v-5M12 16V8M16 16v-8' },
  { id: 'customers', label: 'Customers', title: 'Customer management',
    description: 'Create and verify durable customer records before approved Cognito binding.',
    adminOnly: true,
    icon: 'M7 20v-1a5 5 0 0 1 10 0v1M12 11a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM19 8h3M20.5 6.5v3' },
];

const themeOptions = [
  { id: 'light', label: 'Light', icon: 'M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6 7 7M17 17l1.4 1.4M18.4 5.6 17 7M7 17l-1.4 1.4M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0z' },
  { id: 'dark', label: 'Dark', icon: 'M20 15.3A8 8 0 0 1 8.7 4 8 8 0 1 0 20 15.3z' },
  { id: 'system', label: 'System', icon: 'M4 5h16v11H4zM9 20h6M12 16v4' },
];

function ThemeSwitcher({ value, onChange }) {
  return <div className="theme-switcher" role="group" aria-label="Color theme">
    {themeOptions.map((option) => <button key={option.id} type="button"
      className="theme-option" aria-pressed={value === option.id}
      title={`${option.label} theme`} onClick={() => onChange(option.id)}>
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
        strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
        <path d={option.icon} />
      </svg>
      <span>{option.label}</span>
    </button>)}
  </div>;
}

function GatewayIcon({ children }) {
  return <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{children}</svg>;
}

function PortalArtwork({ type }) {
  if (type === 'admin') {
    return <svg className="portal-artwork" viewBox="0 0 210 122" fill="none" aria-hidden="true">
      <rect x="12" y="10" width="186" height="102" rx="12" className="art-screen" />
      <path d="M12 32h186" className="art-line" /><circle cx="29" cy="21" r="3" className="art-dot" />
      <circle cx="40" cy="21" r="3" className="art-dot art-dot--muted" /><circle cx="51" cy="21" r="3" className="art-dot art-dot--muted" />
      <rect x="29" y="47" width="50" height="7" rx="3.5" className="art-line" /><rect x="29" y="62" width="36" height="5" rx="2.5" className="art-line art-line--muted" />
      <rect x="29" y="76" width="43" height="5" rx="2.5" className="art-line art-line--muted" />
      <path d="M101 91V72M119 91V58M137 91V43M155 91V65" className="art-bars" /><path d="M93 91h74" className="art-line" />
      <path d="M99 57c13-15 25 3 37-13s22 5 34-11" className="art-trend" />
    </svg>;
  }
  return <svg className="portal-artwork" viewBox="0 0 210 122" fill="none" aria-hidden="true">
    <rect x="12" y="10" width="186" height="102" rx="12" className="art-screen art-screen--violet" />
    <path d="M12 32h186" className="art-line art-line--violet" /><circle cx="29" cy="21" r="3" className="art-dot art-dot--violet" />
    <circle cx="40" cy="21" r="3" className="art-dot art-dot--muted" /><circle cx="51" cy="21" r="3" className="art-dot art-dot--muted" />
    <circle cx="41" cy="53" r="11" className="art-avatar" /><path d="M27 80c2-12 26-12 29 0" className="art-line art-line--violet" />
    <path d="M88 48h69M88 62h46M88 76h58" className="art-line art-line--muted" />
    <path d="M142 75h37a7 7 0 0 1 7 7v15a7 7 0 0 1-7 7h-18l-10 8v-8h-9a7 7 0 0 1-7-7V82a7 7 0 0 1 7-7Z" className="art-bubble" />
    <path d="M151 88h20M151 96h13" className="art-line art-line--violet" />
  </svg>;
}

const introWorkflow = [
  { label: 'Customer Request', detail: 'A clear request starts the journey.', tone: 'cyan' },
  { label: 'Case Understanding', detail: 'Context and evidence are organized.', tone: 'cyan' },
  { label: 'Agent Tools', detail: 'Trusted tools gather the right facts.', tone: 'purple' },
  { label: 'AI Investigation', detail: 'Grounded reasoning prepares a recommendation.', tone: 'purple' },
  { label: 'Human Oversight', detail: 'A reviewer remains in control.', tone: 'cyan' },
  { label: 'Resolution Update', detail: 'The customer receives a clear next step.', tone: 'cyan' },
];

function AgenticWelcome({ onEnter }) {
  const [activeStep, setActiveStep] = useState(0);
  const [animationKey, setAnimationKey] = useState(0);
  const [isPlaying, setIsPlaying] = useState(true);
  const [showDemo, setShowDemo] = useState(false);
  const isComplete = activeStep === introWorkflow.length - 1;
  const progress = ((activeStep + 1) / introWorkflow.length) * 100;
  const currentStep = introWorkflow[activeStep];

  useEffect(() => {
    if (!isPlaying || isComplete) return undefined;
    const timer = globalThis.setInterval(() => {
      setActiveStep((step) => Math.min(step + 1, introWorkflow.length - 1));
    }, 1150);
    return () => globalThis.clearInterval(timer);
  }, [animationKey, isComplete, isPlaying]);

  function replay() {
    setActiveStep(0);
    setIsPlaying(true);
    setAnimationKey((key) => key + 1);
  }

  function togglePlay() {
    if (isComplete) {
      replay();
      return;
    }
    setIsPlaying((playing) => !playing);
  }

  return <main className="agentic-welcome" aria-labelledby="agentic-welcome-title">
    <div className="agentic-welcome__aurora agentic-welcome__aurora--cyan" aria-hidden="true" />
    <div className="agentic-welcome__aurora agentic-welcome__aurora--purple" aria-hidden="true" />
    <div className="agentic-welcome__grid" aria-hidden="true" />
    <div className="agentic-welcome__content">
      <div className="agentic-welcome__brand">
        <div className="agentic-welcome__logo-shell"><img src={logoUrl} alt="NovaMind AI" className="agentic-welcome__logo" width="230" height="115" /></div>
        <p className="agentic-welcome__credit">Built by Aamir</p>
        <p className="agentic-welcome__kicker">AWS-POWERED AGENTIC AI</p>
        <h1 id="agentic-welcome-title">Every case, understood.</h1>
        <p className="agentic-welcome__lead">Intelligent customer resolution powered by Amazon Bedrock, Strands Agents, and human oversight.</p>
      </div>
      <div className="agentic-welcome__workflow" aria-label="NovaMind AI workflow" aria-live="polite">
        <div className="agentic-welcome__workflow-line" aria-hidden="true"><span style={{ '--workflow-progress': `${(activeStep / (introWorkflow.length - 1)) * 100}%` }} /></div>
        {introWorkflow.map((step, index) => <div key={step.label} className={`agentic-step agentic-step--${step.tone}${index <= activeStep ? ' is-active' : ''}${index === activeStep ? ' is-current' : ''}`}>
          <span className="agentic-step__node" aria-hidden="true">{String(index + 1).padStart(2, '0')}</span><span className="agentic-step__label">{step.label}</span><span className="agentic-step__detail">{index === activeStep ? step.detail : index < activeStep ? 'Complete' : 'Queued'}</span>
        </div>)}
      </div>
      <div className="agentic-welcome__stage" role="status" aria-live="polite">
        <div className={`agentic-welcome__stage-orb agentic-welcome__stage-orb--${currentStep.tone}`} aria-hidden="true"><span /></div>
        <div><p className="agentic-welcome__stage-label">{isComplete ? 'Workflow ready' : isPlaying ? 'Current stage' : 'Animation paused'}</p><p className="agentic-welcome__stage-title">{currentStep.label}</p><p className="agentic-welcome__stage-copy">{currentStep.detail}</p></div>
      </div>
      <div className="agentic-welcome__progress" aria-label={`Workflow progress: ${Math.round(progress)} percent`}><div className="agentic-welcome__progress-meta"><span>Workflow progress</span><strong>{Math.round(progress)}%</strong></div><div className="agentic-welcome__progress-track"><span style={{ width: `${progress}%` }} /></div></div>
      <div className="agentic-welcome__actions">
        <button type="button" className="agentic-button agentic-button--primary" onClick={onEnter}>Enter Platform <span aria-hidden="true">&#x2192;</span></button>
        <button type="button" className="agentic-button agentic-button--quiet" onClick={onEnter}>Skip Intro</button>
        <button type="button" className="agentic-button agentic-button--control" onClick={togglePlay} aria-label={isPlaying ? 'Pause workflow animation' : 'Play workflow animation'}>{isPlaying && !isComplete ? 'Pause' : 'Play'} <span aria-hidden="true">{isPlaying && !isComplete ? '\u23f8' : '\u25b6'}</span></button>
        <button type="button" className="agentic-button agentic-button--replay" onClick={replay}>Replay Animation <span aria-hidden="true">&#x21bb;</span></button>
      </div>
      <button type="button" className={`agentic-demo-toggle${showDemo ? ' is-active' : ''}`} onClick={() => setShowDemo((visible) => !visible)} aria-expanded={showDemo}><span className="agentic-demo-toggle__dot" aria-hidden="true" /> {showDemo ? 'Hide' : 'Preview'} simulated agent demonstration</button>
      {showDemo && <div className="agentic-demo" role="note"><div className="agentic-demo__header"><span>SIMULATION MODE</span><span className="agentic-demo__pulse">&#x25cf; local preview</span></div><p><strong>{currentStep.label}</strong> <span>&mdash;</span> {isComplete ? 'Recommendation ready for human oversight.' : 'Agent is coordinating the next grounded step...'}</p><div className="agentic-demo__stream" aria-hidden="true"><i /><i /><i /><i /></div></div>}
      <p className="agentic-welcome__safety">EVIDENCE-DRIVEN <span>&bull;</span> POLICY-GROUNDED <span>&bull;</span> HUMAN-APPROVED</p>
    </div>
  </main>;
}

function PortalGateway({ screen, status, message, themePreference, onThemeChange, onChoose, onBack,
  onSubmit, cognitoConfigured, onCognitoLogin }) {
  const isLogin = screen === 'admin' || screen === 'customer';
  const isCustomerLogin = screen === 'customer';
  const title = isCustomerLogin ? 'Your resolution starts here.' : 'Oversee every resolution.';
  const description = cognitoConfigured
    ? 'Continue with your organization account. NovaMind verifies the access token and its role before opening a portal.'
    : isCustomerLogin
      ? 'Report an issue, follow its progress, and track your resolution updates in one place.'
      : 'Review AI-assisted investigation, policy checks, and human approval in one operations workspace.';

  return <div className="gateway-shell">
    <a className="skip-link" href="#portal-content">Skip to portal content</a>
    <header className="gateway-topbar">
      <a className="gateway-brand" href="#portal-content" aria-label="NovaMind AI home">
        <span className="gateway-logo-lockup">
          <img src={logoUrl} alt="NovaMind AI" width="160" height="80" />
          <span>Built by Aamir</span>
        </span>
        <span className="gateway-brand-divider" aria-hidden="true" />
        <span className="gateway-brand-copy">
          <span>AI-assisted customer resolution</span>
          <strong>NovaMind AI Customer<br />Resolution Platform</strong>
        </span>
      </a>
      <ThemeSwitcher value={themePreference} onChange={onThemeChange} />
    </header>

    <main id="portal-content" className="gateway-content" tabIndex={-1}>
      {!isLogin ? <>
        <section className="gateway-hero" aria-labelledby="gateway-title">
          <h1 id="gateway-title"><span>AI-Powered</span> Customer Resolution</h1>
          <p className="gateway-lead">
            Investigate customer complaints with case evidence and policy checks, orchestrate AI-assisted
            workflows with LangGraph and Amazon Bedrock, then keep consequential decisions under human approval.
          </p>
          <div className="gateway-capabilities" aria-label="Platform capabilities">
            <span>Evidence-led investigation</span><span>Policy eligibility</span><span>Human review</span>
          </div>
        </section>

        <section className="portal-choice-grid" aria-label="Choose a portal">
          <article className="portal-choice portal-choice--staff">
            <span className="portal-choice-icon" aria-hidden="true"><GatewayIcon><path d="M4 20v-9l8-6 8 6v9M9 20v-5h6v5M3 20h18" /></GatewayIcon></span>
            <PortalArtwork type="admin" />
            <p className="eyebrow">Operations team</p>
            <h2>Admin Portal</h2>
            <p>Monitor cases, guide AI workflows, review proposals, and inspect audit and diagnostic data.</p>
            <button type="button" onClick={() => onChoose('admin')}>Open Admin Portal <span aria-hidden="true">→</span></button>
            <small>Reviewer accounts open the staff workspace with their permitted tools.</small>
          </article>
          <article className="portal-choice portal-choice--customer">
            <span className="portal-choice-icon" aria-hidden="true"><GatewayIcon><path d="M6 3h12l2 5v13H4V8l2-5ZM4 8h16M9 12a3 3 0 0 0 6 0" /></GatewayIcon></span>
            <PortalArtwork type="customer" />
            <p className="eyebrow">My account</p>
            <h2>Customer Portal</h2>
            <p>Browse the catalog, create demo orders, report an issue, and follow your own support requests.</p>
            <button type="button" className="button-secondary" onClick={() => onChoose('customer')}>Open Customer Portal <span aria-hidden="true">→</span></button>
            <small>Only records returned for the authenticated customer are displayed.</small>
          </article>
        </section>

        <section className="gateway-process" aria-labelledby="process-title">
          <div><p className="eyebrow">How it works</p><h2 id="process-title">From issue to resolution</h2></div>
          <ol>
            <li className="process-step process-step--customer"><strong>1</strong><span className="process-icon"><GatewayIcon><path d="M5 4h14v12H9l-4 4zM8 8h7M8 12h4" /></GatewayIcon></span><div><b>Customer Issue</b><span>Customer submits details and supporting evidence.</span></div></li>
            <li className="process-arrow" aria-hidden="true">→</li>
            <li className="process-step process-step--ai"><strong>2</strong><span className="process-icon"><GatewayIcon><path d="M7 7h10v10H7zM10 3v4M14 3v4M10 17v4M14 17v4M3 10h4M17 10h4M3 14h4M17 14h4" /></GatewayIcon></span><div><b>AI Analysis</b><span>AI checks evidence and policy before preparing a recommendation.</span></div></li>
            <li className="process-arrow" aria-hidden="true">→</li>
            <li className="process-step process-step--human"><strong>3</strong><span className="process-icon"><GatewayIcon><path d="M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8ZM4 21a8 8 0 0 1 16 0" /></GatewayIcon></span><div><b>Human Approval</b><span>A trained reviewer considers and approves the final decision.</span></div></li>
          </ol>
        </section>
      </> : <section className="portal-login" aria-labelledby="portal-login-title">
        <button type="button" className="gateway-back" onClick={onBack}><span aria-hidden="true">←</span> All portals</button>
        <span className="portal-login-icon" aria-hidden="true"><GatewayIcon><path d="M12 3a5 5 0 0 0-5 5v3H5v10h14V11h-2V8a5 5 0 0 0-5-5Zm-3 8V8a3 3 0 0 1 6 0v3" /></GatewayIcon></span>
        <p className="eyebrow">{cognitoConfigured ? 'Secure organization sign-in' : 'Secure local sign-in'}</p>
        <h1 id="portal-login-title">{title}</h1>
        <p>{description}</p>
        {cognitoConfigured ? <div className="portal-login-form">
          <p id="portal-login-status" className={`portal-login-status ${status}`} role="status" aria-live="polite">{message}</p>
          <button type="button" onClick={onCognitoLogin} disabled={status === 'checking' || !onCognitoLogin}>
            {status === 'checking' ? 'Continuing securely…' : onCognitoLogin ? 'Continue with organization sign-in' : 'Organization sign-in unavailable'}
          </button>
          <p className="portal-login-help">A short-lived authorization code is exchanged with Cognito using PKCE. Your access token is held only in this page’s memory.</p>
        </div> : <form className="portal-login-form" onSubmit={onSubmit} autoComplete="off">
          <label htmlFor="local-access-token">Access token
            <input id="local-access-token" name="token" type="password" required minLength={16} maxLength={256}
              autoComplete="off" spellCheck={false} autoCapitalize="none" disabled={status === 'checking'}
              aria-describedby="portal-login-help portal-login-status" />
          </label>
          <p id="portal-login-status" className={`portal-login-status ${status}`} role="status" aria-live="polite">{message}</p>
          <button type="submit" disabled={status === 'checking'}>
            {status === 'checking' ? 'Verifying identity…' : `Continue to ${isCustomerLogin ? 'Customer' : 'Staff'} Portal`}
          </button>
          <p id="portal-login-help" className="portal-login-help">Your token is verified by the local API and held only in this page’s memory. Reloading or signing out clears it.</p>
        </form>}
      </section>}
    </main>
    <footer className="gateway-footer">Local demonstration environment <span aria-hidden="true">·</span> API identity is the source of role access</footer>
  </div>;
}

function App() {
  const [cognitoAuthentication] = useState(() => {
    try {
      return { configuration: readCognitoConfiguration(), error: null };
    } catch {
      return { configuration: null, error: 'Organization sign-in is misconfigured. Contact an administrator.' };
    }
  });
  const cognitoConfiguration = cognitoAuthentication.configuration;
  const [health, setHealth] = useState('idle');
  const [activeView, setActiveView] = useState('cases');
  const [session, setSession] = useState(null);
  const [sessionVersion, setSessionVersion] = useState(0);
  const [identityStatus, setIdentityStatus] = useState('signed-out');
  const [identityMessage, setIdentityMessage] = useState(() => cognitoAuthentication.error
    || (cognitoConfiguration
      ? 'Continue with your organization account to confirm your identity.'
      : 'Enter your existing local access token to confirm your identity.'));
  const [themePreference, setThemePreference] = useState(() => readThemePreference());
  const [systemDark, setSystemDark] = useState(() => globalThis.matchMedia?.('(prefers-color-scheme: dark)').matches ?? true);
  const [mobileNavOpen, setMobileNavOpen] = useState(false);
  const [portalScreen, setPortalScreen] = useState('landing');
  const [showIntro, setShowIntro] = useState(true);
  const identityClient = useRef(null);

  // Derive whether this session is a customer; drives what is shown/hidden.
  const isCustomer = session?.identity?.role === 'CUSTOMER';
  const portalName = isCustomer ? 'Customer Portal' : session?.identity?.role === 'REVIEWER' ? 'Staff Portal' : 'Admin Portal';
  const availableStaffViews = staffViews.filter((entry) => entry.id !== 'diagnostics'
    || session?.identity?.can_view_diagnostics === true).filter((entry) => entry.id !== 'architecture'
    || session?.identity?.role === 'ADMIN').filter((entry) => entry.id !== 'analytics'
    || session?.identity?.role === 'ADMIN').filter((entry) => !entry.adminOnly
    || session?.identity?.role === 'ADMIN');

  // Staff view state — only relevant when not a customer.
  const view = availableStaffViews.find((entry) => entry.id === activeView) ?? availableStaffViews[0];

  useEffect(() => {
    const query = globalThis.matchMedia?.('(prefers-color-scheme: dark)');
    if (!query) return undefined;
    const update = (event) => setSystemDark(event.matches);
    query.addEventListener?.('change', update);
    return () => query.removeEventListener?.('change', update);
  }, []);

  useEffect(() => {
    storeThemePreference(themePreference);
    applyTheme(resolveTheme(themePreference, systemDark));
  }, [themePreference, systemDark]);

  useEffect(() => {
    setMobileNavOpen(false);
  }, [activeView, isCustomer]);

  useEffect(() => {
    if (!cognitoConfiguration || !hasCognitoCallback()) return undefined;
    // OAuth replaces the React document. Recover only the chosen presentation
    // flow; the verified API identity still decides what workspace is rendered.
    const intendedPortal = readCognitoPortalIntent(cognitoConfiguration) ?? 'admin';
    setPortalScreen(intendedPortal);
    setIdentityStatus('checking');
    setIdentityMessage('Completing organization sign-in…');
    finishCognitoLogin(cognitoConfiguration).then((token) => {
      completeIdentity(token, 'cognito', intendedPortal);
    }).catch(() => {
      clearIdentity();
      setPortalScreen(intendedPortal);
      setIdentityStatus('error');
      setIdentityMessage(`Organization sign-in could not be completed. Try again from the ${intendedPortal === 'customer' ? 'Customer' : 'Admin & Reviewer'} Portal.`);
    });
    return undefined;
  }, [cognitoConfiguration]);

  function clearIdentity() {
    identityClient.current?.dispose();
    identityClient.current = null;
    setSession(null);
    // Drop case data across identities; ordinary view navigation still preserves it.
    setSessionVersion((version) => version + 1);
  }

  function signOut() {
    clearIdentity();
    setIdentityStatus('signed-out');
    setIdentityMessage(cognitoConfiguration
      ? 'Signing out of your organization account. Submitted runs are not cancelled.'
      : 'Signed out locally. Credentials and workspace data have been cleared. Submitted runs are not cancelled.');
    setPortalScreen('landing');
    if (cognitoConfiguration) signOutOfCognito(cognitoConfiguration);
  }

  async function confirmIdentity(event) {
    event.preventDefault();
    if (identityStatus === 'checking') return;
    const form = event.currentTarget;
    const token = new FormData(form).get('token');
    form.reset();
    await completeIdentity(token, 'local', portalScreen === 'customer' ? 'customer' : 'admin');
  }

  function startCognitoLogin() {
    if (!cognitoConfiguration || identityStatus === 'checking') return;
    const intendedPortal = portalScreen === 'customer' ? 'customer' : 'admin';
    setIdentityStatus('checking');
    setIdentityMessage('Redirecting to organization sign-in…');
    try {
      rememberCognitoPortal(cognitoConfiguration, intendedPortal);
    } catch {
      setPortalScreen(intendedPortal);
      setIdentityStatus('error');
      setIdentityMessage('Organization sign-in could not be started. Try again or contact an administrator.');
      return;
    }
    beginCognitoLogin(cognitoConfiguration).catch(() => {
      setPortalScreen(intendedPortal);
      setIdentityStatus('error');
      setIdentityMessage('Organization sign-in could not be started. Try again or contact an administrator.');
    });
  }

  async function completeIdentity(token, source, intendedPortal = 'admin') {
    clearIdentity();
    setIdentityStatus('checking');
    const organizationSignIn = source === 'cognito';
    setIdentityMessage(organizationSignIn
      ? 'Confirming your identity with the API…'
      : 'Confirming your identity with the local API…');
    const candidate = createApi(globalThis.fetch, {
      token,
      onUnauthorized() {
        if (identityClient.current !== candidate) return;
        clearIdentity();
        setPortalScreen(intendedPortal);
        setIdentityStatus('error');
        setIdentityMessage(organizationSignIn
          ? 'Authentication required. Sign in again with your organization account.'
          : 'Authentication required. Enter a valid token configured for the local API.');
      },
    });
    identityClient.current = candidate;
    try {
      const identity = await candidate.identity();
      // Ignore responses from a cancelled or replaced identity request.
      if (identityClient.current !== candidate) return;
      setSession({ identity, api: candidate });
      setSessionVersion((version) => version + 1);
      setIdentityStatus('authenticated');
      if (organizationSignIn) clearCognitoPortalIntent(cognitoConfiguration);
      const customer = identity.role === 'CUSTOMER';
      setActiveView(customer ? 'cases' : 'dashboard');
      const verifiedBy = organizationSignIn ? 'the API' : 'the local API';
      setIdentityMessage(customer
        ? `Identity confirmed by ${verifiedBy}. Customer Portal opened.`
        : `Identity confirmed by ${verifiedBy}. ${identity.role === 'REVIEWER' ? 'Staff' : 'Admin'} Portal opened.`);
    } catch (error) {
      if (identityClient.current !== candidate) return;
      clearIdentity();
      setPortalScreen(intendedPortal);
      setIdentityStatus('error');
      setIdentityMessage(error.status === 403
        ? `Access denied by ${organizationSignIn ? 'the API' : 'the local API'}.`
        : `Identity could not be confirmed. Check ${organizationSignIn ? 'the API' : 'the local API'} and try again.`);
    }
  }

  async function checkHealth() {
    setHealth('checking');
    try {
      const response = await fetch('/api/health', {
        signal: AbortSignal.timeout(5000),
        cache: 'no-store',
      });
      if (!response.ok) throw new Error('API request failed');
      const data = await response.json();
      if (data.status !== 'ok' || data.service !== 'novamind-api') {
        throw new Error('Unexpected health response');
      }
      setHealth('online');
    } catch {
      setHealth('offline');
    }
  }

  const messages = {
    idle: 'Connection not checked. Check the API when you\'re ready to load case data.',
    checking: 'Checking the local API\u2026',
    online: 'The local API is responding. This check confirms connectivity only.',
    offline: 'The local API is unavailable. Check that the backend is running, then try again.',
  };

  if (!session) {
    if (showIntro && portalScreen === 'landing') {
      return <AgenticWelcome onEnter={() => setShowIntro(false)} />;
    }
    return <PortalGateway
      screen={portalScreen}
      status={identityStatus}
      message={identityMessage}
      themePreference={themePreference}
      onThemeChange={setThemePreference}
      onChoose={setPortalScreen}
      onBack={() => { if (cognitoConfiguration) clearCognitoPortalIntent(cognitoConfiguration); setPortalScreen('landing'); setIdentityStatus('signed-out'); setIdentityMessage(cognitoAuthentication.error
        || (cognitoConfiguration ? 'Continue with your organization account to confirm your identity.'
          : 'Enter your existing local access token to confirm your identity.')); }}
      onSubmit={confirmIdentity}
      cognitoConfigured={cognitoConfiguration !== null || cognitoAuthentication.error !== null}
      onCognitoLogin={cognitoConfiguration ? startCognitoLogin : null}
    />;
  }

  return (
    <div className="app-shell">
      <a className="skip-link" href="#workspace">Skip to workspace</a>

      {/* ── Sidebar ─────────────────────────────────────────────────── */}
      <aside className={`sidebar${mobileNavOpen ? ' sidebar--open' : ''}`} aria-label="Workspace navigation">

        <div className="sidebar-head">
          <a className="brand" href="#workspace" aria-label="NovaMind AI — go to workspace">
            <img src={logoUrl} alt="NovaMind AI" className="brand-logo" width="130" height="65" />
            <span className="brand-text">
              <span className="brand-name">NovaMind <span className="brand-ai">AI</span></span>
              <span className="brand-caption">Customer Resolution Platform</span>
              <span className="brand-credit">Built by Aamir</span>
            </span>
          </a>
          <button type="button" className="mobile-nav-toggle" aria-expanded={mobileNavOpen}
            aria-controls="workspace-navigation" onClick={() => setMobileNavOpen((open) => !open)}>
            <span className="sr-only">{mobileNavOpen ? 'Close navigation' : 'Open navigation'}</span>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"
              strokeLinecap="round" aria-hidden="true">
              {mobileNavOpen ? <path d="M6 6l12 12M18 6 6 18" /> : <path d="M4 7h16M4 12h16M4 17h16" />}
            </svg>
          </button>
        </div>

        {/* Navigation */}
        <nav id="workspace-navigation" className={`workspace-nav${mobileNavOpen ? ' workspace-nav--open' : ''}`}
          aria-label="Main navigation">
          {isCustomer ? (
            <>
              <p className="nav-label">My account</p>
              <div className="nav-items">
                <span className="nav-item nav-item--customer-label">
                  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
                    strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
                    <circle cx="12" cy="8" r="4" />
                    <path d="M4 20c0-4 3.6-7 8-7s8 3 8 7" />
                  </svg>
                  Customer portal
                </span>
              </div>
            </>
          ) : (
            <>
              <p className="nav-label">{portalName}</p>
              <div className="nav-items">
                {availableStaffViews.map((entry) => (
                  <button key={entry.id} type="button" className="nav-item"
                    aria-current={activeView === entry.id ? 'page' : undefined}
                    aria-controls="workspace"
                    onClick={() => { setActiveView(entry.id); setMobileNavOpen(false); }}>
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6"
                      strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
                      <path d={entry.icon} />
                    </svg>
                    {entry.label}
                    {activeView === entry.id && (
                      <span className="nav-indicator" aria-hidden="true" />
                    )}
                  </button>
                ))}
              </div>
            </>
          )}
        </nav>

        {/* Sidebar footer — context + attribution + social links */}
        <div className="sidebar-footer">
          {/* Current workspace context */}
          <div className="sidebar-context">
            <span className="context-symbol" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5"
                strokeLinejoin="round" strokeLinecap="round" aria-hidden="true">
                <path d="M12 2a10 10 0 1 0 10 10A10 10 0 0 0 12 2zm0 4v4l3 3" />
              </svg>
            </span>
            <div>
              <strong>Local workspace</strong>
              <span>Customer support</span>
            </div>
          </div>

          <div className="sidebar-attribution">
            <div className="sidebar-social">
              <a
                href="https://github.com/aamir490"
                target="_blank"
                rel="noopener noreferrer"
                className="social-link"
                aria-label="Aamir on GitHub">
                {/* GitHub icon */}
                <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M12 2C6.477 2 2 6.477 2 12c0 4.418 2.865 8.166 6.839 9.489.5.092.682-.217.682-.483 0-.237-.009-.868-.013-1.703-2.782.605-3.369-1.342-3.369-1.342-.454-1.154-1.11-1.462-1.11-1.462-.908-.62.069-.608.069-.608 1.003.07 1.531 1.03 1.531 1.03.892 1.529 2.341 1.087 2.91.832.092-.647.35-1.088.636-1.338-2.22-.253-4.555-1.11-4.555-4.943 0-1.091.39-1.984 1.029-2.683-.103-.253-.446-1.27.098-2.647 0 0 .84-.269 2.75 1.025A9.578 9.578 0 0 1 12 6.836a9.59 9.59 0 0 1 2.504.337c1.909-1.294 2.747-1.025 2.747-1.025.546 1.377.202 2.394.1 2.647.64.699 1.028 1.592 1.028 2.683 0 3.842-2.339 4.687-4.566 4.935.359.309.678.919.678 1.852 0 1.336-.012 2.415-.012 2.743 0 .267.18.579.688.481C19.138 20.163 22 16.418 22 12c0-5.523-4.477-10-10-10z"/>
                </svg>
              </a>
              <a
                href="https://www.linkedin.com/in/aamir-imran"
                target="_blank"
                rel="noopener noreferrer"
                className="social-link"
                aria-label="Aamir on LinkedIn">
                {/* LinkedIn icon */}
                <svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">
                  <path d="M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433a2.062 2.062 0 0 1-2.063-2.065 2.064 2.064 0 1 1 2.063 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z"/>
                </svg>
              </a>
            </div>
          </div>
        </div>
      </aside>

      {/* ── App body ────────────────────────────────────────────────── */}
      <div className="app-body">

        {/* Topbar */}
        <header className="topbar">
          <p className="breadcrumb">
            <span>{portalName}</span>
            <span aria-hidden="true">/</span>
            {isCustomer ? 'Customer portal' : view.label}
          </p>
          <div className="topbar-end">
            <span className="environment-label">Local environment</span>
            <ThemeSwitcher value={themePreference} onChange={setThemePreference} />
          </div>
        </header>

        {/* Main workspace */}
        <main id="workspace" className="workspace" tabIndex={-1}>

          {/* Identity panel */}
          <section className={`identity-panel${session ? ' identity-panel--authenticated' : ''}`} aria-labelledby="identity-title">
            <div className="identity-heading">
              <h2 id="identity-title">Workspace identity</h2>
              <span className={`badge ${session ? 'identity-role' : ''}`}>
                {session
                  ? session.identity.role
                  : identityStatus === 'checking' ? 'Verifying' : 'Not signed in'}
              </span>
            </div>
            <p id="identity-status" className={`identity-status ${identityStatus}`}
              role="status" aria-live="polite">
              {identityMessage}
            </p>
            {session
              ? <div className="identity-details">
                  <details className="identity-technical">
                    <summary>Identity details</summary>
                    <dl>
                      <dt>User ID</dt><dd>{session.identity.user_id}</dd>
                      {session.identity.customer_id && (
                        <><dt>Customer ID</dt><dd>{session.identity.customer_id}</dd></>
                      )}
                    </dl>
                  </details>
                  <button type="button" className="button-secondary" onClick={signOut}>Sign out</button>
                </div>
              : <form className="identity-form" onSubmit={confirmIdentity} autoComplete="off">
                  <label htmlFor="local-access-token">Local access token
                    <input id="local-access-token" name="token" type="password"
                      required minLength={16} maxLength={256}
                      autoComplete="off" spellCheck={false} autoCapitalize="none"
                      disabled={identityStatus === 'checking'}
                      aria-describedby="identity-help identity-status" />
                  </label>
                  <div className="identity-actions">
                    <button type="submit" disabled={identityStatus === 'checking'}>
                      {identityStatus === 'checking' ? 'Verifying\u2026' : 'Confirm identity'}
                    </button>
                    {identityStatus === 'checking' && (
                      <button type="button" className="button-secondary" onClick={signOut}>
                        Cancel
                      </button>
                    )}
                  </div>
                  <p id="identity-help" className="identity-help">
                    Kept in memory for this page only. Reloading or signing out clears it.
                  </p>
                </form>
            }
          </section>

          {/* Page heading */}
          <div className="page-heading">
            <div>
              <p className="eyebrow">{isCustomer ? 'NovaMind AI' : 'Customer resolution'}</p>
              <h1 aria-live="polite">{isCustomer ? 'Customer Dashboard' : view.title}</h1>
              <p className="intro">
                {isCustomer
                  ? 'Browse products, place a demo order, and track your orders.'
                  : view.description}
              </p>
            </div>
            <span className="badge">
              {isCustomer
                ? 'Customer portal'
                : ['cases', 'audit', 'diagnostics'].includes(activeView)
                  ? 'Read-only workspace'
                  : activeView === 'reviews'
                    ? 'Human review'
                    : activeView === 'ai-workspace'
                      ? 'Live run tracking'
                      : activeView === 'dashboard'
                        ? 'Operations overview'
                        : activeView === 'customers'
                          ? 'Admin only'
                        : 'Placeholder view'}
            </span>
          </div>

          {/* Customer portal — shown only to CUSTOMER role */}
          {isCustomer && (
            <CustomerPortal key={sessionVersion} api={session.api} identity={session.identity} />
          )}

          {/* Staff workspace — shown only to non-CUSTOMER roles */}
          {!isCustomer && <>
            {view.placeholder && (
              <section className="view-placeholder" aria-labelledby="placeholder-title">
                <p className="eyebrow">{view.label}</p>
                <h2 id="placeholder-title">This view is not connected yet.</h2>
                <p>{view.placeholder}</p>
              </section>
            )}
            {activeView === 'dashboard' && (
              <Dashboard
                key={sessionVersion}
                api={session?.api}
                identity={session?.identity}
                onNavigate={setActiveView}
              />
            )}
            <div hidden={activeView !== 'ai-workspace'}>
              <AIWorkspace key={sessionVersion} api={session?.api} identity={session?.identity} />
            </div>
            <div hidden={activeView !== 'reviews'}>
              <ReviewQueue key={sessionVersion} api={session?.api} identity={session?.identity}
                active={activeView === 'reviews'} />
            </div>
            {activeView === 'audit' && (
              <AuditWorkspace key={sessionVersion} api={session?.api} identity={session?.identity} />
            )}
            {activeView === 'diagnostics' && (
              <DiagnosticsWorkspace key={sessionVersion} api={session?.api} identity={session?.identity} />
            )}
            {activeView === 'architecture' && (
              <ArchitectureExplorer key={sessionVersion} api={session?.api} identity={session?.identity} />
            )}
            {activeView === 'analytics' && (
              <AnalyticsWorkspace key={sessionVersion} api={session?.api} identity={session?.identity} />
            )}
            {activeView === 'customers' && session?.identity?.role === 'ADMIN' && (
              <CustomerProvisioning key={sessionVersion} api={session.api} />
            )}
            {/* Keep the case workspace mounted so navigation preserves an in-progress review. */}
            <div className="workspace-grid cases-layout" hidden={activeView !== 'cases'}>
              <CaseReview key={sessionVersion} api={session?.api} identity={session?.identity} />
              <aside className="workspace-rail" aria-label="Workspace information">
                <section className="connection-panel" aria-labelledby="connection-title">
                  <div className="panel-heading">
                    <span className="panel-icon" aria-hidden="true">
                      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5">
                        <rect x="4" y="4" width="16" height="6" rx="2" />
                        <rect x="4" y="14" width="16" height="6" rx="2" />
                        <path d="M8 7h.01M8 17h.01M12 7h5M12 17h5" strokeLinecap="round" />
                      </svg>
                    </span>
                    <div>
                      <p className="eyebrow">Connection</p>
                      <h2 id="connection-title">Local API</h2>
                    </div>
                  </div>
                  <p className={`status ${health}`} role="status" aria-live="polite">
                    {messages[health]}
                  </p>
                  <button className="button-secondary" onClick={checkHealth}
                    disabled={health === 'checking'}>
                    {health === 'checking' ? 'Checking\u2026' : 'Check local API'}
                  </button>
                </section>
                <section className="guidance-panel" aria-labelledby="boundary-title">
                  <p className="eyebrow">Review boundary</p>
                  <h2 id="boundary-title">Evidence before action.</h2>
                  <p>Eligibility is not authorization. Approval is not execution.</p>
                  <span className="guidance-rule" aria-hidden="true" />
                  <p className="footnote">
                    This workspace supports read-only assessment. No business action is performed here.
                  </p>
                </section>
              </aside>
            </div>
          </>}

          {/* Workspace footer */}
          <footer className="workspace-footer">
            <span>
              NovaMind AI
              <span aria-hidden="true"> / </span>
              Customer Resolution Platform
            </span>
            <span className="workspace-footer-links">
              <a href="https://github.com/aamir490"
                target="_blank" rel="noopener noreferrer"
                className="footer-link">
                GitHub
              </a>
              <span aria-hidden="true">&middot;</span>
              <a href="https://www.linkedin.com/in/aamir-imran"
                target="_blank" rel="noopener noreferrer"
                className="footer-link">
                LinkedIn
              </a>
            </span>
            <span>
              {isCustomer
                ? 'Customer portal'
                : activeView === 'cases'
                  ? 'Local case review'
                  : 'Local workspace'}
            </span>
          </footer>

        </main>
      </div>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<StrictMode><App /></StrictMode>);
