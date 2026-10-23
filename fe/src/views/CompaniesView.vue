<script setup>
import { computed, inject } from 'vue'

import CompanyCard from '../components/CompanyCard.vue'
import FilterChips from '../components/FilterChips.vue'
import FilterPanel from '../components/FilterPanel.vue'
import Pagination from '../components/Pagination.vue'
import { apiUrl, formatNumber, humanize } from '../api.js'
import { useFetch } from '../composables/useFetch.js'
import { useQueryState } from '../composables/useQueryState.js'

const { filters: facets, hasAi } = inject('facets')

const SORTS = [
	{ value: '-n_product_releases', label: 'Most product releases' },
	{ value: 'name', label: 'Name A–Z' },
	{ value: '-last_product_release', label: 'Most recent release' },
	{ value: 'founded_year', label: 'Oldest founding year' },
]

const state = useQueryState({
	q: 'one', is_public: 'one', has_ticker: 'one', company: 'one',
	min_releases: 'one', max_releases: 'one', founded_from: 'one', founded_to: 'one',
	sort: 'one', page: 'one',
	org_type: 'list', country: 'list',
})
const { filters, query, set, setSoon, toggle, clear } = state

const { data, loading, error, reload } = useFetch('/api/companies', () => query.value)

const page = computed(() => Number(filters.value.page) || 1)

const fields = computed(() => {
	const values = facets.value || {}
	return [
		{ kind: 'text', key: 'q', label: 'Name', placeholder: 'Company name' },
		{
			kind: 'multi', key: 'org_type', label: 'Organisation type',
			options: (values.org_types || []).map((o) => ({ value: o, label: humanize(o) })),
		},
		{
			kind: 'choice', key: 'is_public', label: 'Publicly listed',
			options: [
				{ value: null, label: 'Any' },
				{ value: 'true', label: 'Public' },
				{ value: 'false', label: 'Private' },
				// Its own state, never folded into Private: it means the
				// evidence did not decide.
				{ value: 'unknown', label: 'Unknown' },
			],
		},
		{
			kind: 'multi', key: 'country', label: 'Country',
			options: (values.countries || []).map((c) => ({ value: c, label: c })),
		},
		{ kind: 'bool', key: 'has_ticker', label: 'Has a stock ticker' },
		{
			kind: 'numrange', key: 'releases', label: 'Product releases',
			from: 'min_releases', to: 'max_releases', min: 0,
			minPlaceholder: 'min', maxPlaceholder: 'max',
		},
		{
			kind: 'numrange', key: 'founded', label: 'Founded',
			from: 'founded_from', to: 'founded_to', min: 1000, max: 2200,
			minPlaceholder: 'from', maxPlaceholder: 'to',
		},
	]
})

const PUBLIC_LABELS = { true: 'Public', false: 'Private', unknown: 'Unknown' }

const chips = computed(() => {
	const active = filters.value
	const out = []
	if (active.company) {
		out.push({
			id: 'company', label: 'Company',
			value: data.value?.items?.[0]?.name || decodeURIComponent(active.company),
			patch: { company: null },
		})
	}
	if (active.q) out.push({ id: 'q', label: 'Name', value: active.q, patch: { q: null } })
	for (const type of active.org_type) {
		out.push({ id: `org:${type}`, label: 'Type', value: humanize(type), patch: { org_type: active.org_type.filter((o) => o !== type) } })
	}
	for (const code of active.country) {
		out.push({ id: `country:${code}`, label: 'Country', value: code, patch: { country: active.country.filter((c) => c !== code) } })
	}
	if (active.is_public) out.push({ id: 'pub', label: 'Listed', value: PUBLIC_LABELS[active.is_public], patch: { is_public: null } })
	if (active.has_ticker) out.push({ id: 'ticker', label: 'Ticker', value: 'Has one', patch: { has_ticker: null } })
	if (active.min_releases) out.push({ id: 'min', label: 'Releases', value: `≥ ${active.min_releases}`, patch: { min_releases: null } })
	if (active.max_releases) out.push({ id: 'max', label: 'Releases', value: `≤ ${active.max_releases}`, patch: { max_releases: null } })
	if (active.founded_from) out.push({ id: 'ff', label: 'Founded', value: `from ${active.founded_from}`, patch: { founded_from: null } })
	if (active.founded_to) out.push({ id: 'ft', label: 'Founded', value: `to ${active.founded_to}`, patch: { founded_to: null } })
	return out
})

const relax = computed(() => {
	const order = ['q', 'ff', 'ft', 'min', 'max', 'country']
	const first = order.map((id) => chips.value.find((c) => c.id === id)).find(Boolean)
	return first || chips.value[0]
})

const csvHref = computed(() => {
	const { page: _page, per_page: _perPage, ...rest } = query.value
	return apiUrl('/api/companies.csv', rest)
})
</script>

<template>
	<div class="layout">
		<FilterPanel :fields="fields" :values="filters"
			@update="set" @update-soon="setSoon" @toggle="toggle" />

		<section class="results">
			<div class="toolbar">
				<FilterChips class="toolbar__chips" :chips="chips" :total="data?.total ?? null"
					noun="company" plural="companies" :loading="loading"
					@remove="set($event.patch)" @clear="clear" />
				<div class="toolbar__controls">
					<label class="sr-only" for="sort">Sort</label>
					<select id="sort" class="input" :value="filters.sort || '-n_product_releases'"
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
				<p>Could not load companies: {{ error }}</p>
				<button class="button" type="button" @click="reload">Retry</button>
			</div>

			<div v-else-if="loading && !data" class="grid" aria-busy="true">
				<div v-for="n in 6" :key="n" class="skeleton card-skeleton" />
			</div>

			<p v-else-if="data && !data.items.length" class="panel notice">
				<template v-if="relax">
					No companies match these filters. Try removing the
					<strong>{{ relax.label.toLowerCase() }}</strong> filter&nbsp;<button class="linkish" type="button" @click="set(relax.patch)">({{ relax.value }})</button>.
				</template>
				<template v-else>
					No companies yet — a company appears here once it has a product release.
				</template>
			</p>

			<div v-else class="grid" :aria-busy="loading" :class="{ 'grid--stale': loading }">
				<CompanyCard v-for="company in data.items" :key="company._id"
					:company="company" :has-ai="hasAi" @blacklisted="reload" />
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
.grid--stale { opacity: 0.5; transition: opacity 120ms ease-out; }
.card-skeleton { height: 380px; }

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
