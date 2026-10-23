<script setup>
import { computed, inject } from 'vue'

import BarChart from '../components/BarChart.vue'
import Badge from '../components/Badge.vue'
import StatTile from '../components/StatTile.vue'
import TimeSeries from '../components/TimeSeries.vue'
import { DATE, MONTH, formatDate, formatNumber, humanize } from '../api.js'
import { useFetch } from '../composables/useFetch.js'

const { hasAi } = inject('facets')
const { data, loading, error, reload } = useFetch('/api/overview')

const span = computed(() => {
	if (!data.value?.published_from) return '—'
	return `${formatDate(data.value.published_from, MONTH)} – ${formatDate(data.value.published_to, MONTH)}`
})

const aiShare = computed(() => {
	const total = data.value?.n_releases
	if (!total || data.value.n_ai_releases === null) return null
	return `${Math.round((data.value.n_ai_releases / total) * 100)}%`
})

const categories = computed(() => {
	const rows = (data.value?.by_category || []).map((c) => ({ label: humanize(c.key), n: c.n }))
	// The top ten are the chart; the tail is one bar so the reader can see how
	// much of the corpus it is rather than having to infer it.
	if (data.value?.other_categories > 0) rows.push({ label: 'Other', n: data.value.other_categories })
	return rows
})

const orgTypes = computed(() =>
	(data.value?.by_org_type || []).map((o) => ({ label: humanize(o.key) || 'Unclassified', n: o.n })))

const models = computed(() => {
	const served = data.value?.models || {}
	return ['launch', 'ai'].map((task) => ({ task, run: served[task] }))
})

const PERCENT = (value) =>
	value === null || value === undefined ? '—' : `${(value * 100).toFixed(1)}%`
</script>

<template>
	<div v-if="error" class="panel error" role="alert">
		<p>Could not load the overview: {{ error }}</p>
		<button class="button" type="button" @click="reload">Retry</button>
	</div>

	<template v-else>
		<section class="tiles" :class="{ 'tiles--three': !hasAi }" aria-label="Dataset at a glance">
			<StatTile label="Product releases" :loading="loading"
				:value="formatNumber(data?.n_releases)"
				hint="Releases the launch classifier called relevant" />
			<StatTile label="Companies" :loading="loading"
				:value="formatNumber(data?.n_companies)"
				hint="Senders with at least one product release" />
			<StatTile label="Covered period" :loading="loading" :value="span"
				hint="By publication date" />
			<!-- Dropped entirely, rather than shown as zero, when no `ai` run is
			     served: every release's is_ai is null, and 0% would be a claim. -->
			<StatTile v-if="hasAi" label="Shipping generative AI" :loading="loading"
				:value="aiShare ?? '—'"
				:hint="`${formatNumber(data?.n_ai_releases)} of ${formatNumber(data?.n_releases)} releases`" />
		</section>

		<TimeSeries class="section" :series="data?.series || []" :show-ai="hasAi" :loading="loading" />

		<div class="section split">
			<BarChart title="Releases by PRN category"
				caption="Top ten categories, with the remainder grouped."
				:items="categories" :loading="loading" />
			<BarChart title="Releases by sender type"
				caption="Most senders are operating companies; the tail is the interesting part."
				:items="orgTypes" :loading="loading" />
		</div>

		<div class="section split">
			<section class="panel list">
				<h2>Latest releases</h2>
				<ol v-if="!loading" class="latest">
					<li v-for="release in data?.latest || []" :key="release._id">
						<a :href="release.url" target="_blank" rel="noopener noreferrer">{{ release.title }}</a>
						<p class="small">
							<RouterLink v-if="release.company_id"
								:to="{ path: '/companies', query: { company: release.company_id } }">
								{{ release.company_name }}
							</RouterLink>
							<span class="muted"> · {{ formatDate(release.published_at, DATE) }}</span>
							<Badge v-if="release.is_ai" tone="ai">Generative AI</Badge>
						</p>
					</li>
				</ol>
				<div v-else class="skeleton list__skeleton" />
				<p v-if="!loading && !data?.latest?.length" class="muted small">
					No product releases yet.
				</p>
			</section>

			<section class="panel list">
				<h2>Most active companies</h2>
				<div class="table__scroll">
					<table v-if="!loading">
						<thead>
							<tr>
								<th scope="col">Company</th>
								<th scope="col">Type</th>
								<th scope="col">Releases</th>
								<th scope="col">Latest</th>
							</tr>
						</thead>
						<tbody>
							<tr v-for="company in data?.top_companies || []" :key="company._id">
								<th scope="row">
									<RouterLink :to="{ path: '/products', query: { company: company._id } }">
										{{ company.name }}
									</RouterLink>
								</th>
								<td class="muted">{{ humanize(company.org_type) }}</td>
								<td class="tabular">{{ formatNumber(company.n_product_releases) }}</td>
								<td class="muted">{{ formatDate(company.last_product_release, DATE) }}</td>
							</tr>
						</tbody>
					</table>
					<div v-else class="skeleton list__skeleton" />
				</div>
			</section>
		</div>

		<section class="panel provenance section">
			<h2>Dataset provenance</h2>
			<p class="small muted">
				Every figure above is the output of these runs. Cite them alongside any slice
				you export; a rerun changes the population, not just the counts.
			</p>
			<div class="table__scroll">
				<table>
					<thead>
						<tr>
							<th scope="col">Head</th>
							<th scope="col">Version</th>
							<th scope="col">Trained</th>
							<th scope="col">Accuracy</th>
							<th scope="col">ROC-AUC</th>
							<th scope="col">FPR</th>
							<th scope="col">FNR</th>
						</tr>
					</thead>
					<tbody>
						<tr v-for="{ task, run } in models" :key="task">
							<th scope="row">{{ task }}</th>
							<template v-if="run">
								<td><code>{{ run.version }}</code></td>
								<td>{{ formatDate(run.trained_at, DATE) }}</td>
								<td class="tabular">{{ PERCENT(run.metrics.eval_accuracy) }}</td>
								<td class="tabular">{{ PERCENT(run.metrics.eval_roc_auc) }}</td>
								<td class="tabular">{{ PERCENT(run.metrics.eval_false_positive_rate) }}</td>
								<td class="tabular">{{ PERCENT(run.metrics.eval_false_negative_rate) }}</td>
							</template>
							<td v-else class="muted" colspan="6">
								No run served — nothing in this app is flagged on this axis.
							</td>
						</tr>
					</tbody>
				</table>
			</div>
			<p v-if="data?.built_at" class="small muted">
				Read model built {{ formatDate(data.built_at, DATE) }} from
				<code>predictions</code>, <code>articles</code> and <code>companies</code>.
			</p>
		</section>
	</template>
</template>

<style scoped>
.tiles {
	display: grid;
	grid-template-columns: repeat(4, minmax(0, 1fr));
	gap: var(--space-4);
}
.tiles--three { grid-template-columns: repeat(3, minmax(0, 1fr)); }

.section { margin-top: var(--space-5); }
.split {
	display: grid;
	grid-template-columns: repeat(2, minmax(0, 1fr));
	gap: var(--space-4);
	align-items: start;
}

.list { padding: var(--space-4) var(--space-5) var(--space-5); }
.list h2 { margin-bottom: var(--space-3); }
.list__skeleton { height: 300px; }

.latest { margin: 0; padding: 0 0 0 var(--space-5); }
.latest li { padding: var(--space-2) 0; border-bottom: 1px solid var(--border); }
.latest li:last-child { border-bottom: 0; }
.latest a { color: var(--ink); text-decoration: none; font-weight: 500; }
.latest a:hover { text-decoration: underline; text-decoration-color: var(--accent); }
.latest p { margin: var(--space-1) 0 0; display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: center; }

.table__scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
th, td { text-align: left; padding: 6px var(--space-3); border-bottom: 1px solid var(--border); white-space: nowrap; }
thead th { color: var(--ink-muted); font-weight: 600; }
tbody th { font-weight: 500; white-space: normal; }
tbody tr:last-child th, tbody tr:last-child td { border-bottom: 0; }
code { font-size: var(--text-xs); }

.provenance { padding: var(--space-4) var(--space-5) var(--space-5); }
.provenance p { margin: var(--space-2) 0; max-width: 78ch; }

.error { padding: var(--space-5); display: flex; gap: var(--space-4); align-items: center; }
.error p { margin: 0; }

@media (max-width: 1000px) {
	.tiles, .tiles--three { grid-template-columns: repeat(2, minmax(0, 1fr)); }
	.split { grid-template-columns: minmax(0, 1fr); }
}
@media (max-width: 560px) {
	.tiles, .tiles--three { grid-template-columns: minmax(0, 1fr); }
}
</style>
