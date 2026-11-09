"""Shared reCAPTCHA execution helpers for local browser providers."""

import json


TRUSTED_RECAPTCHA_HOOK_SOURCE = r"""
(() => {
    if (window.__flow2apiTrustedRecaptchaHookInstalled) return;
    Object.defineProperty(
        window,
        '__flow2apiTrustedRecaptchaHookInstalled',
        { value: true }
    );

    const storageKey = '__flow2apiTrustedRecaptchaExecute';
    const originalBind = Function.prototype.bind;
    Function.prototype.bind = function(thisArg, ...args) {
        const bound = originalBind.call(this, thisArg, ...args);
        try {
            const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
            if (
                !window[storageKey] &&
                enterprise &&
                this === enterprise.execute &&
                typeof bound === 'function'
            ) {
                window[storageKey] = bound;
            }
        } catch (error) {}
        return bound;
    };

    // New Flow replaces the public execute wrapper with an assignment that
    // overwrites the caller action by extension_hijack_detected. Preserve the
    // requested action when that exact assignment shape is used.
    Object.defineProperty(
        window,
        '__flow2apiRecaptchaActionGuardInstalled',
        { value: true }
    );
    const originalAssign = Object.assign;
    Object.assign = function(target, ...sources) {
        let nextSources = sources;
        if (sources.length >= 2) {
            const previous = sources[sources.length - 2];
            const last = sources[sources.length - 1];
            if (
                previous &&
                typeof previous.action === 'string' &&
                previous.action &&
                last &&
                last.action === 'extension_hijack_detected'
            ) {
                nextSources = sources.slice(0, -1);
            }
        }
        return originalAssign.call(this, target, ...nextSources);
    };
})();
"""

RECAPTCHA_EXECUTE_SOURCE = r"""(async (siteKey, requestedAction, timeoutMs) => {
            const storageKey = '__flow2apiTrustedRecaptchaExecute';
            const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));

            const runExecute = (execute, target) => new Promise((resolve, reject) => {
                let settled = false;
                const finish = (handler, value) => {
                    if (settled) return;
                    settled = true;
                    handler(value);
                };
                const timer = setTimeout(() => {
                    finish(reject, new Error('execute timeout'));
                }, timeoutMs);
                try {
                    Promise.resolve(execute(target, { action: requestedAction }))
                        .then(value => {
                            clearTimeout(timer);
                            finish(resolve, value);
                        })
                        .catch(error => {
                            clearTimeout(timer);
                            finish(reject, error);
                        });
                } catch (error) {
                    clearTimeout(timer);
                    finish(reject, error);
                }
            });
            const errText = error => (error && error.message ? error.message : String(error));

            // Primary: the page's own execute called directly with the site key (no waiting).
            // Fallback: the bound execute captured by the Function.prototype.bind hook.
            const enterprise = window.grecaptcha && window.grecaptcha.enterprise;
            const direct = enterprise && enterprise.execute;
            let directSource = '';
            try { directSource = String(direct || ''); } catch (error) {}
            const guardMissing =
                directSource.includes('extension_hijack_detected') &&
                !window.__flow2apiRecaptchaActionGuardInstalled;

            let directError = '';
            if (typeof direct === 'function' && !guardMissing) {
                try {
                    const token = await runExecute(direct, siteKey);
                    if (token) return { ok: true, token, via: 'direct' };
                    directError = 'empty token';
                } catch (error) {
                    directError = errText(error);
                }
            } else if (guardMissing) {
                directError = 'trusted reCAPTCHA action guard is unavailable';
            } else {
                directError = 'reCAPTCHA execute is unavailable';
            }

            for (let attempt = 0; attempt < 40; attempt += 1) {
                if (window[storageKey]) break;
                await sleep(250);
            }
            const captured = window[storageKey];
            if (typeof captured !== 'function') {
                return { ok: false, error: 'direct execute failed: ' + directError };
            }
            let target = siteKey;
            if (window.__flow2apiRecaptchaClientId !== undefined) {
                target = window.__flow2apiRecaptchaClientId;
            }
            try {
                const token = await runExecute(captured, target);
                return { ok: true, token, via: 'captured' };
            } catch (error) {
                return {
                    ok: false,
                    error: errText(error) + ' | direct: ' + directError
                        + ' | ' + JSON.stringify({
                            actionGuard: !!window.__flow2apiRecaptchaActionGuardInstalled,
                            url: location.href
                        })
                };
            }
        })(__SITE_KEY__, __REQUESTED_ACTION__, __TIMEOUT_MS__)"""


def build_recaptcha_execute_script(site_key: str, action: str, timeout_ms: int) -> str:
    return (
        RECAPTCHA_EXECUTE_SOURCE
        .replace("__SITE_KEY__", json.dumps(site_key))
        .replace("__REQUESTED_ACTION__", json.dumps(action))
        .replace("__TIMEOUT_MS__", json.dumps(timeout_ms))
    )
