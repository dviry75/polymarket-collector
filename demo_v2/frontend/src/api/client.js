/** All browser networking and DEMO session/CSRF state lives here. No durable credentials. */
export class ApiError extends Error {
  /** @param {number} status @param {string} code @param {unknown} [details] */
  constructor(status, code, details) { super(code); this.name = 'ApiError'; this.status = status; this.code = code; this.details = details; }
}
/** @param {string} [base] @param {typeof fetch} [transport] @param {()=>void} [onUnauthorized] */
export function createClient(base = '/demo/api/v1', transport = globalThis.fetch.bind(globalThis), onUnauthorized = () => {}) {
  const url = new URL(base, 'http://relative.invalid');
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || url.search || url.hash || base.startsWith('//')) throw new Error('Invalid public DEMO_API_BASE');
  let csrf = '';
  /** @param {string} path @param {{method?:string,body?:unknown,signal?:AbortSignal,authentication?:boolean}} [options] @returns {Promise<any>} */
  async function request(path, { method = 'GET', body, signal, authentication = true } = {}) {
    const headers = new Headers({ Accept: 'application/json' });
    if (body !== undefined) headers.set('Content-Type', 'application/json');
    if (method !== 'GET' && authentication) {
      if (!csrf) throw new ApiError(403, 'DEMO_CSRF_REQUIRED');
      headers.set('X-Demo-CSRF', csrf);
    }
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 12000);
    const abort = () => controller.abort();
    signal?.addEventListener('abort', abort, { once: true });
    if (signal?.aborted) controller.abort();
    try {
      const response = await transport(base.replace(/\/$/, '') + path, {
        method, headers, credentials: 'include', cache: 'no-store', signal: controller.signal,
        ...(body === undefined ? {} : { body: JSON.stringify(body) })
      });
      let payload;
      try { payload = await response.json(); } catch { throw new ApiError(response.status, 'INVALID_API_RESPONSE'); }
      if (!response.ok) {
        if (response.status === 401 && authentication) { csrf = ''; onUnauthorized(); }
        throw new ApiError(response.status, payload?.detail?.code || `HTTP_${response.status}`, payload?.detail);
      }
      return payload;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      throw new ApiError(0, controller.signal.aborted ? 'REQUEST_ABORTED' : 'NETWORK_ERROR');
    } finally { clearTimeout(timeout); signal?.removeEventListener('abort', abort); }
  }
  /** @param {any} value */
  function session(value) {
    if (typeof value?.user !== 'string' || typeof value?.csrf_token !== 'string' || !value.csrf_token) throw new ApiError(502, 'INVALID_SESSION_RESPONSE');
    csrf = value.csrf_token; return value;
  }
  /** @param {Record<string,string>} query */
  const queryString = query => { const value = new URLSearchParams(query).toString(); return value ? '?' + value : ''; };
  const id = encodeURIComponent;
  return {
    request,
    current: async () => session(await request('/auth/current')),
    /** @param {string} username @param {string} password */
    login: async (username, password) => session(await request('/auth/login', { method: 'POST', body: { username, password }, authentication: false })),
    logout: async () => { const result = await request('/auth/logout', { method: 'POST' }); csrf = ''; return result; },
    clearSession: () => { csrf = ''; },
    rules: () => request('/rules'),
    /** @param {string} ruleId */
    rule: ruleId => request('/rules/' + id(ruleId)),
    /** @param {unknown} spec */
    createRule: spec => request('/rules', { method: 'POST', body: spec }),
    /** @param {string} ruleId @param {unknown} spec @param {string} version */
    updateRule: (ruleId, spec, version) => request('/rules/' + id(ruleId), { method: 'PUT', body: { spec, expected_version_id: version } }),
    /** @param {string} ruleId @param {'enable'|'disable'|'duplicate'|'archive'} action */
    ruleAction: (ruleId, action) => request('/rules/' + id(ruleId) + '/' + action, { method: 'POST' }),
    /** @param {Record<string,string>} query @param {AbortSignal} [signal] */
    trades: (query, signal) => request('/trades' + queryString(query), { signal }),
    /** @param {string} tradeId */
    trade: tradeId => request('/trades/' + id(tradeId)),
    /** @param {Record<string,string>} query @param {AbortSignal} [signal] */
    kpi: (query, signal) => request('/kpi' + queryString(query), { signal }),
    markets: () => request('/markets'),
    health: () => request('/health'),
    /** Confirmation must come from an explicit UI confirmation action. @param {'RESET_TRADING_DATA'|'FACTORY_RESET_DEMO'} confirmation */
    reset: confirmation => request('/maintenance/' + (confirmation === 'RESET_TRADING_DATA' ? 'reset-trading-data' : 'factory-reset'), { method: 'POST', body: { confirmation } })
  };
}
/** @param {unknown} error */
export function errorMessage(error) {
  if (!(error instanceof ApiError)) return 'הפעולה נכשלה. אפשר לנסות שוב.';
  if (error.status === 401) return error.code === 'DEMO_INVALID_CREDENTIALS' ? 'שם המשתמש או הסיסמה שגויים.' : 'הסשן פג. יש להתחבר מחדש.';
  if (error.status === 409) return 'הנתונים השתנו או שהאישור אינו תקין. יש לרענן לפני ניסיון נוסף; השינויים בטופס נשמרו.';
  if (error.status === 403) return 'אימות הפעולה נכשל. יש להתחבר מחדש.';
  if (error.status === 429) return 'יותר מדי ניסיונות. יש להמתין לפני ניסיון נוסף.';
  if (error.status === 422) return 'ההגדרות לא התקבלו. בדקו את הערכים בטופס. (' + error.code + ')';
  if (!error.status) return 'אין חיבור לשרת או שהבקשה לא הושלמה בזמן. נתונים קודמים אינם עדכניים.';
  return 'השרת אינו זמין כרגע (' + error.code + '). אפשר לנסות שוב.';
}
