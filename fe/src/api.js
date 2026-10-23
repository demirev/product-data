// The API client. Every call is cancellable: filter changes fire faster than
// Mongo answers, and an out-of-order response would show the previous filter's
// results under the current chips.

export function apiUrl(path, params = {}) {
	const search = new URLSearchParams()
	for (const [key, value] of Object.entries(params)) {
		if (value === null || value === undefined || value === '') continue
		for (const item of [].concat(value)) search.append(key, item)
	}
	const query = search.toString()
	return query ? `${path}?${query}` : path
}

export async function fetchJson(path, params, signal) {
	const response = await fetch(apiUrl(path, params), { signal })
	if (!response.ok) {
		// The API answers a bad filter with {"detail": "..."}; anything else
		// (a proxy, a crash) only has a status to report.
		const detail = await response.json().catch(() => null)
		throw new Error(detail?.detail || `Request failed (${response.status})`)
	}
	return response.json()
}

// Puts a sender on the blacklist, or takes it off. Throws on any non-2xx.
export async function setBlacklisted(companyId, blacklisted, reason = null) {
	const response = await fetch('/api/blacklist', {
		method: 'POST',
		headers: { 'Content-Type': 'application/json' },
		body: JSON.stringify({ company_id: companyId, blacklisted, reason }),
	})
	if (!response.ok) throw new Error(`Could not save (${response.status})`)
	return response.json()
}

export const DATE = new Intl.DateTimeFormat('en-GB', {
	year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC',
})
export const MONTH = new Intl.DateTimeFormat('en-GB', {
	year: 'numeric', month: 'short', timeZone: 'UTC',
})

export function formatDate(value, format = DATE) {
	return value ? format.format(new Date(value)) : ''
}

export function formatNumber(value) {
	return value === null || value === undefined ? '' : value.toLocaleString('en-US')
}

// PRN category slugs and org_type values are both lower-snake/kebab identifiers
// that read fine as words once the separators go.
export function humanize(value) {
	if (!value) return ''
	return value.replace(/[-_]/g, ' ').replace(/(^|\s)\w/g, (c) => c.toUpperCase())
}
