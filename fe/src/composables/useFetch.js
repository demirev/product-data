import { ref, shallowRef, watch } from 'vue'

import { fetchJson } from '../api.js'

// One request per resource in flight at a time. `request()` returns a source of
// truth for the three states every list and chart in the app has to render —
// loading, error, data — so no view reimplements them, and a superseded request
// is aborted rather than allowed to land after its replacement.
export function useFetch(path, params) {
	const data = shallowRef(null)
	const loading = ref(true)
	const error = ref(null)
	let controller = null

	async function load() {
		controller?.abort()
		controller = new AbortController()
		const mine = controller
		loading.value = true
		error.value = null
		try {
			data.value = await fetchJson(path, params ? params() : {}, mine.signal)
			error.value = null
		} catch (failure) {
			if (failure.name === 'AbortError') return  // a newer request owns the state
			error.value = failure.message
		} finally {
			if (mine === controller) loading.value = false
		}
	}

	if (params) watch(params, load, { immediate: true, deep: true })
	else load()

	return { data, loading, error, reload: load }
}
