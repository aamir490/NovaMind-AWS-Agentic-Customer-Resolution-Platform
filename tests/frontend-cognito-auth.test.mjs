import assert from 'node:assert/strict';
import test from 'node:test';

import {
  beginCognitoLogin, clearCognitoPortalIntent, finishCognitoLogin,
  readCognitoConfiguration, readCognitoPortalIntent, rememberCognitoPortal,
  signOutOfCognito,
} from '../frontend/src/cognitoAuth.js';

function browser(href = 'https://app.example.test/portal') {
  const values = new Map();
  const location = {
    href,
    assign(value) { this.href = value; },
  };
  return {
    location,
    sessionStorage: {
      getItem: (key) => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: (key) => values.delete(key),
    },
    history: { replaceState: (_state, _title, value) => { location.href = value; } },
    values,
  };
}

const environment = {
  VITE_COGNITO_DOMAIN: 'https://novamind.auth.us-east-1.amazoncognito.com',
  VITE_COGNITO_CLIENT_ID: 'phaseBClient_123',
  VITE_COGNITO_REDIRECT_URI: 'https://app.example.test/portal',
  VITE_COGNITO_LOGOUT_URI: 'https://app.example.test/portal',
};

test('Hosted UI login uses authorization code, PKCE S256, and an opaque state', async () => {
  const page = browser();
  const configuration = readCognitoConfiguration(environment, page);
  await beginCognitoLogin(configuration, page);
  const authorize = new URL(page.location.href);
  assert.equal(authorize.pathname, '/oauth2/authorize');
  assert.equal(authorize.searchParams.get('response_type'), 'code');
  assert.equal(authorize.searchParams.get('code_challenge_method'), 'S256');
  assert.equal(authorize.searchParams.get('client_id'), environment.VITE_COGNITO_CLIENT_ID);
  assert.ok(authorize.searchParams.get('state'));
  assert.ok(authorize.searchParams.get('code_challenge'));
  assert.equal(page.values.size, 2);
});

test('PKCE token exchange validates state and keeps the access token out of storage and URLs', async () => {
  const page = browser();
  const configuration = readCognitoConfiguration(environment, page);
  await beginCognitoLogin(configuration, page);
  const authorize = new URL(page.location.href);
  page.location.href = `https://app.example.test/portal?code=authorization-code&state=${encodeURIComponent(authorize.searchParams.get('state'))}`;
  let request;
  const token = await finishCognitoLogin(configuration, page, async (url, options) => {
    request = { url, options };
    return { ok: true, json: async () => ({ access_token: 'a'.repeat(32) }) };
  });
  assert.equal(token, 'a'.repeat(32));
  assert.equal(new URL(request.url).pathname, '/oauth2/token');
  assert.equal(request.options.method, 'POST');
  assert.equal(request.options.credentials, 'omit');
  assert.match(request.options.body.toString(), /code_verifier=/);
  assert.equal(page.values.size, 0);
  assert.equal(new URL(page.location.href).search, '');
});

test('customer portal intent survives the OAuth round trip but never becomes an authorization claim', async () => {
  const page = browser();
  const configuration = readCognitoConfiguration(environment, page);
  rememberCognitoPortal(configuration, 'customer', page);
  await beginCognitoLogin(configuration, page);
  const authorize = new URL(page.location.href);
  assert.equal(readCognitoPortalIntent(configuration, page), 'customer');
  assert.equal(authorize.searchParams.get('state').includes('customer'), false);
  page.location.href = `https://app.example.test/portal?code=authorization-code&state=${encodeURIComponent(authorize.searchParams.get('state'))}`;
  await finishCognitoLogin(configuration, page, async () => ({ ok: true, json: async () => ({ access_token: 'a'.repeat(32) }) }));
  assert.equal(readCognitoPortalIntent(configuration, page), 'customer');
  clearCognitoPortalIntent(configuration, page);
  assert.equal(readCognitoPortalIntent(configuration, page), null);
  assert.throws(() => rememberCognitoPortal(configuration, 'ADMIN', page));
});

test('state mismatch fails before exchange, and logout never needs a client secret', async () => {
  const page = browser();
  const configuration = readCognitoConfiguration(environment, page);
  await beginCognitoLogin(configuration, page);
  page.location.href = 'https://app.example.test/portal?code=authorization-code&state=wrong';
  await assert.rejects(finishCognitoLogin(configuration, page, async () => {
    throw new Error('exchange must not run');
  }));
  signOutOfCognito(configuration, page);
  const logout = new URL(page.location.href);
  assert.equal(logout.pathname, '/logout');
  assert.equal(logout.searchParams.get('client_id'), environment.VITE_COGNITO_CLIENT_ID);
  assert.equal(logout.searchParams.get('client_secret'), null);
});

test('Cognito configuration fails closed when only part of it is supplied', () => {
  assert.throws(() => readCognitoConfiguration({ VITE_COGNITO_DOMAIN: environment.VITE_COGNITO_DOMAIN }, browser()));
  assert.throws(() => readCognitoConfiguration({ ...environment, VITE_COGNITO_DOMAIN: 'http://unsafe.example.test' }, browser()));
});
