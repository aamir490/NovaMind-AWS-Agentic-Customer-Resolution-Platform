// Hosted UI authorization-code + PKCE support.  This module intentionally has
// no AWS SDK dependency and never persists the resulting access token.

const maximumTokenLength = 16 * 1024;
const configurationPrefix = 'novamind.cognito.';

function requiredText(value, name, pattern) {
  if (typeof value !== 'string' || !pattern.test(value)) {
    throw new Error(`Invalid ${name} configuration`);
  }
  return value;
}

function browserLocation(browser) {
  if (!browser?.location?.href) throw new Error('A browser location is required');
  return new URL(browser.location.href);
}

function redirectUri(value, fallback, name) {
  const url = new URL(value || fallback);
  const local = url.hostname === 'localhost' || url.hostname === '127.0.0.1';
  if ((url.protocol !== 'https:' && !(local && url.protocol === 'http:'))
      || url.username || url.password || url.hash) {
    throw new Error(`Invalid ${name} configuration`);
  }
  return url.href;
}

function base64url(bytes) {
  let binary = '';
  bytes.forEach((value) => { binary += String.fromCharCode(value); });
  return globalThis.btoa(binary).replaceAll('+', '-').replaceAll('/', '_').replaceAll('=', '');
}

function randomValue() {
  const bytes = new Uint8Array(32);
  globalThis.crypto.getRandomValues(bytes);
  return base64url(bytes);
}

function storageKeys(configuration) {
  const key = `${configurationPrefix}${configuration.clientId}.`;
  return { verifier: `${key}verifier`, state: `${key}state`, portal: `${key}portal` };
}

function portalIntent(value) {
  if (value !== 'admin' && value !== 'customer') {
    throw new Error('Invalid portal sign-in intent');
  }
  return value;
}

// This is presentation state only. The API derives all actual access from the
// verified Cognito access token; a selected portal can never grant a role.
export function rememberCognitoPortal(configuration, portal, browser = globalThis) {
  browser.sessionStorage.setItem(storageKeys(configuration).portal, portalIntent(portal));
}

export function readCognitoPortalIntent(configuration, browser = globalThis) {
  const value = browser.sessionStorage.getItem(storageKeys(configuration).portal);
  return value === 'admin' || value === 'customer' ? value : null;
}

export function clearCognitoPortalIntent(configuration, browser = globalThis) {
  browser.sessionStorage.removeItem(storageKeys(configuration).portal);
}

export function readCognitoConfiguration(environment = import.meta.env, browser = globalThis) {
  const values = [
    environment.VITE_COGNITO_DOMAIN,
    environment.VITE_COGNITO_CLIENT_ID,
    environment.VITE_COGNITO_REDIRECT_URI,
    environment.VITE_COGNITO_LOGOUT_URI,
  ];
  if (values.every((value) => value === undefined || value === '')) return null;
  if (values.slice(0, 2).some((value) => value === undefined || value === '')) {
    throw new Error('Cognito domain and client ID must be configured together');
  }
  const domain = new URL(environment.VITE_COGNITO_DOMAIN);
  if (domain.protocol !== 'https:' || domain.username || domain.password || domain.port
      || domain.pathname !== '/' || domain.search || domain.hash) {
    throw new Error('Invalid Cognito Hosted UI domain configuration');
  }
  const location = browserLocation(browser);
  const fallback = `${location.origin}${location.pathname}`;
  return Object.freeze({
    domain: domain.origin,
    clientId: requiredText(environment.VITE_COGNITO_CLIENT_ID, 'Cognito client ID', /^[A-Za-z0-9_-]{1,128}$/),
    redirectUri: redirectUri(environment.VITE_COGNITO_REDIRECT_URI, fallback, 'Cognito redirect URI'),
    logoutUri: redirectUri(environment.VITE_COGNITO_LOGOUT_URI, fallback, 'Cognito logout URI'),
  });
}

export async function beginCognitoLogin(configuration, browser = globalThis) {
  const verifier = randomValue();
  const state = randomValue();
  const digest = await globalThis.crypto.subtle.digest('SHA-256', new TextEncoder().encode(verifier));
  const keys = storageKeys(configuration);
  browser.sessionStorage.setItem(keys.verifier, verifier);
  browser.sessionStorage.setItem(keys.state, state);
  const authorize = new URL('/oauth2/authorize', configuration.domain);
  authorize.search = new URLSearchParams({
    response_type: 'code', client_id: configuration.clientId,
    redirect_uri: configuration.redirectUri, code_challenge: base64url(new Uint8Array(digest)),
    code_challenge_method: 'S256', state, scope: 'openid',
  }).toString();
  browser.location.assign(authorize.href);
}

export function hasCognitoCallback(browser = globalThis) {
  const parameters = browserLocation(browser).searchParams;
  return parameters.has('code') || parameters.has('error') || parameters.has('state');
}

export async function finishCognitoLogin(configuration, browser = globalThis, fetcher = globalThis.fetch) {
  const current = browserLocation(browser);
  const code = current.searchParams.get('code');
  const error = current.searchParams.get('error');
  const returnedState = current.searchParams.get('state');
  const keys = storageKeys(configuration);
  const expectedState = browser.sessionStorage.getItem(keys.state);
  const verifier = browser.sessionStorage.getItem(keys.verifier);
  browser.sessionStorage.removeItem(keys.state);
  browser.sessionStorage.removeItem(keys.verifier);
  // Remove authorization parameters before any API call or UI error message.
  current.search = '';
  browser.history.replaceState(null, '', current.href);
  if (error || !code || !expectedState || !verifier || returnedState !== expectedState) {
    throw new Error('Cognito authorization was not accepted');
  }
  const response = await fetcher(`${configuration.domain}/oauth2/token`, {
    method: 'POST', cache: 'no-store', credentials: 'omit',
    headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
    body: new URLSearchParams({
      grant_type: 'authorization_code', client_id: configuration.clientId,
      code, redirect_uri: configuration.redirectUri, code_verifier: verifier,
    }),
  });
  if (!response.ok) throw new Error('Cognito token exchange failed');
  const payload = await response.json();
  const token = payload?.access_token;
  if (typeof token !== 'string' || token.length < 16 || token.length > maximumTokenLength) {
    throw new Error('Cognito did not return an access token');
  }
  return token;
}

export function signOutOfCognito(configuration, browser = globalThis) {
  const keys = storageKeys(configuration);
  browser.sessionStorage.removeItem(keys.state);
  browser.sessionStorage.removeItem(keys.verifier);
  browser.sessionStorage.removeItem(keys.portal);
  const logout = new URL('/logout', configuration.domain);
  logout.search = new URLSearchParams({
    client_id: configuration.clientId, logout_uri: configuration.logoutUri,
  }).toString();
  browser.location.assign(logout.href);
}
