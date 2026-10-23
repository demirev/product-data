<script setup>
import { computed, inject } from 'vue'

import FilterChips from '../components/FilterChips.vue'
import FilterPanel from '../components/FilterPanel.vue'
import Pagination from '../components/Pagination.vue'
import ReleaseCard from '../components/ReleaseCard.vue'
import { apiUrl, formatNumber, humanize } from '../api.js'
import { useFetch } from '../composables/useFetch.js'
import { useQueryState } from '../composables/useQueryState.js'

const { filters: facets, hasAi } = inject('facets')

const SORTS = [
	{ value: '-published_at', label: 'Newest first' },
	{ value: 'published_at', label: 'Oldest first' },
	{ value: '-confidence', label: 'Highest confidence' },
]

const state = useQueryState({
	q: 'one', from: 'one', to: 'one', company: 'one', ai: 'one',
	min_confidence: 'one', is_public: 'one', sort: 'one', page: 'one',
	category: 'list', org_type: 'list',
})
const { filters, query, set, setSoon, toggle, clear } = state

const { data, loading, error, reload } = useFetch('/api/releases', () => query.value)

const page = computed(() => Number(filters.value.page) || 1)

const fields = computed(() => {
	const values = facets.value || {}
	return [
		{ kind: 'text', key: 'q', label: 'Search', placeholder: 'Headline or summary text' },
		{
			kind: 'daterange', key: 'dates', label: 'Published',
			min: (values.published_from || '').slice(0, 10),
			max: (values.published_to || '').slice(0, 10),
		},
		{
			kind: 'multi', key: 'category', label: 'PRN category',
			options: (values.categories || []).map((c) => ({ value: c, label: humanize(c) })),
		},
		{ kind: 'company', key: 'company', label: 'Company', placeholder: 'Search senders' },
		...(hasAi.value ? [{ kind: 'bool', key: 'ai', label: 'Only releases shipping generative AI' }] : []),
		{ kind: 'slider', key: 'min_confidence', label: 'Minimum launch confidence' },
		{
			kind: 'multi', key: 'org_type', label: 'Sender type',
			options: (values.org_types || []).map((o) => ({ value: o, label: humanize(o) })),
		},
		{
			kind: 'choice', key: 'is_public', label: 'Sender is publicly listed',
			options: [
				{ value: null, label: 'Any' },
				{ value: 'true', label: 'Public' },
				{ value: 'false', label: 'Private' },
				{ value: 'unknown', label: 'Unknown' },
			],
		},
	]
})

// The company filter is an id; its readable name comes off the results, since
// that is where the read model already denormalized it. A shared URL whose
// filters exclude every one of that company's releases falls back to the id.
const companyName = computed(() => {
	if (!filters.value.company) return null
	const match = (data.value?.items || []).find((r) => r.company_id === filters.value.company)
	return match ? match.company_name : decodeURIComponent(filters.value.company)
})

const PUBLIC_LABELS = { true: 'Public', false: 'Private', unknown: 'Unknown' }

const chips = computed(() => {
	const active = filters.value
	const out = []
	if (active.q) out.push({ id: 'q', label: 'Search', value: active.q, patch: { q: null } })
	if (active.from) out.push({ id: 'from', label: 'From', value: active.from, patch: { from: null } })
	if (active.to) out.push({ id: 'to', label: 'To', value: active.to, patch: { to: null } })
	if (active.company) out.push({ id: 'company', label: 'Company', value: companyName.value, patch: { company: null } })
	for (const slug of active.category) {
		out.push({ id: `category:${slug}`, label: 'Category', value: humanize(slug), patch: { category: active.category.filter((c) => c !== slug) } })
	}
	for (const type of active.org_type) {
		out.push({ id: `org:${type}`, label: 'Sender', value: humanize(type), patch: { org_type: active.org_type.filter((o) => o !== type) } })
	}
	if (active.ai) out.push({ id: 'ai', label: 'AI', value: 'Generative AI', patch: { ai: null } })
	if (active.min_confidence) {
		out.push({
			id: 'conf', label: 'Confidence',
			value: `≥ ${Math.round(Number(active.min_confidence) * 100)}%`,
			patch: { min_confidence: null },
		})
	}
	if (active.is_public) {
		out.push({ id: 'pub', label: 'Listed', value: PUBLIC_LABELS[active.is_public], patch: { is_public: null } })
	}
	return out
})

// Which filter to drop first when nothing matched: the narrowest one, in the
// order a reader is most likely to have over-tightened it.
const relax = computed(() => {
	const order = ['q', 'conf', 'from', 'to', 'company']
	const first = order.map((id) => chips.value.find((c) => c.id === id)).find(Boolean)
	return first || chips.value[0]
})

const csvHref = computed(() => {
	const { page: _page, per_page: _perPage, ...rest } = query.value
	return apiUrl('/api/releases.csv', rest)
})
</script>

<template>
	<div class="layout">
		<FilterPanel :fields="fields" :values="filters"
			@update="set" @update-soon="setSoon" @toggle="toggle" />

		<section class="results">
			<div class="toolbar">
				<FilterChips class="toolbar__chips" :chips="chips" :total="data?.total ?? null"
					noun="product release" plural="product releases" :loading="loading"
					@remove="set($event.patch)" @clear="clear" />
				<div class="toolbar__controls">
					<label class="sr-only" for="sort">Sort</label>
					<select id="sort" class="input" :value="filters.sort || '-published_at'"
						@change="set({ sort: $event.target.value })">
						<option v-for="option in SORTS" :key="option.value" :value="option.value">
							{{ option.label }}
						</option>
					</select>
					<a class="button" :href="csvHref">
						Download CSV<template v-if="data"> ({{ formatNumber(data.total) }} {{ data.total === 1 ? 'row' : 'rows' }})</template>
					</a>
				</div>
			</div>

			<div v-if="error" class="panel notice" role="alert">
				<p>Could not load releases: {{ error }}</p>
				<button class="button" type="button" @click="reload">Retry</button>
			</div>

			<div v-else-if="loading && !data" class="grid" aria-busy="true">
				<div v-for="n in 6" :key="n" class="skeleton card-skeleton" />
			</div>

			<p v-else-if="data && !data.items.length" class="panel notice">
				<template v-if="relax">
					No product releases match these filters. Try removing the
					<strong>{{ relax.label.toLowerCase() }}</strong> filter&nbsp;<button class="linkish" type="button" @click="set(relax.patch)">({{ relax.value }})</button>.
				</template>
				<template v-else>
					No product releases yet — the launch classifier has not written any positives.
				</template>
			</p>

			<div v-else class="grid" :aria-busy="loading" :class="{ 'grid--stale': loading }">
				<ReleaseCard v-for="release in data.items" :key="release._id" :release="release"
					@blacklisted="reload" />
			</div>

			<Pagination v-if="data" :page="page" :per-page="data.per_page" :total="data.total"
				@go="set({ page: $event })" />
		</section>
	</div>
</template>

<style scoped>
.layout {
	display: grid;
	grid-template-columns: 280px minmax(0, 1fr);
	gap: var(--space-5);
	align-items: start;
}

.toolbar {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-3);
	align-items: flex-start;
	justify-content: space-between;
	margin-bottom: var(--space-4);
}
.toolbar__chips { flex: 1 1 320px; margin-bottom: 0; }
.toolbar__controls { display: flex; gap: var(--space-2); align-items: center; }
.toolbar__controls .input { width: auto; }

.grid {
	display: grid;
	grid-template-columns: repeat(3, minmax(0, 1fr));
	gap: var(--space-4);
	align-items: stretch;
}
/* Results already on screen stay legible while the next page loads, rather
   than flashing an empty state under the new filter chips. */
.grid--stale { opacity: 0.5; transition: opacity 120ms ease-out; }
.card-skeleton { height: 232px; }

.notice { padding: var(--space-5); display: flex; flex-wrap: wrap; gap: var(--space-4); align-items: center; }
.notice p { margin: 0; }
.linkish {
	border: 0; background: none; padding: 0;
	color: var(--accent); text-decoration: underline; cursor: pointer;
}

@media (max-width: 1180px) {
	.grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
@media (max-width: 899px) {
	.layout { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 640px) {
	.grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
