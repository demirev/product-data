import { computed, onUnmounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'

// Text inputs fire far faster than Mongo answers, and every keystroke would
// otherwise be a history entry.
const DEBOUNCE_MS = 300

// Filter state lives in the URL and nowhere else, so a slice can be bookmarked
// and shared, and back/forward restore it for free — there is no second copy to
// fall out of step. The query string doubles as the API's parameter set: what
// the address bar says is literally what is sent.
//
// `spec` names each filter and whether it is repeatable; vue-router hands back a
// string for one occurrence and an array for several, so lists are normalized.
export function useQueryState(spec) {
	const route = useRoute()
	const router = useRouter()
	let timer = null

	const filters = computed(() => {
		const out = {}
		for (const [key, kind] of Object.entries(spec)) {
			const raw = route.query[key]
			if (kind === 'list') out[key] = raw === undefined ? [] : [].concat(raw)
			else out[key] = raw === undefined ? null : raw
		}
		return out
	})

	function navigate(patch, replace) {
		const query = { ...route.query }
		for (const [key, value] of Object.entries(patch)) {
			const empty = value === null || value === undefined || value === ''
				|| (Array.isArray(value) && value.length === 0)
			if (empty) delete query[key]
			else query[key] = value
		}
		// Any change but paging returns to page 1: page 3 of the old filter is
		// rarely page 3 of the new one, and is often past its end.
		if (!('page' in patch)) delete query.page
		router[replace ? 'replace' : 'push']({ query })
	}

	onUnmounted(() => clearTimeout(timer))

	return {
		filters,
		query: computed(() => route.query),
		set(patch) {
			clearTimeout(timer)
			navigate(patch, false)
		},
		// For text and slider input: one history entry per pause in typing.
		setSoon(patch) {
			clearTimeout(timer)
			timer = setTimeout(() => navigate(patch, true), DEBOUNCE_MS)
		},
		toggle(key, value) {
			const current = filters.value[key]
			navigate({
				[key]: current.includes(value)
					? current.filter((v) => v !== value)
					: [...current, value],
			}, false)
		},
		// "Clear all" clears filters. Sort order is a view preference, not a
		// filter, and losing it on every clear is a papercut.
		clear() {
			clearTimeout(timer)
			router.push({ query: route.query.sort ? { sort: route.query.sort } : {} })
		},
	}
}
