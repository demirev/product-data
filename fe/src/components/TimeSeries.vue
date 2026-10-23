<script setup>
import { computed, ref } from 'vue'
import { Line } from 'vue-chartjs'

import { axis, baseOptions, palette, scheme } from '../charts.js'
import { formatNumber } from '../api.js'

const props = defineProps({
	// [{month: "2026-04", n: 93, n_ai: 12}, ...], oldest first.
	series: { type: Array, default: () => [] },
	showAi: Boolean,
	loading: Boolean,
})

const GRAINS = [
	{ key: 'month', label: 'Month' },
	{ key: 'quarter', label: 'Quarter' },
	{ key: 'year', label: 'Year' },
]
const grain = ref('month')

// A month with no releases is absent from the API's answer, and a category axis
// would simply not draw it — compressing the gap and making the line lie about
// when things happened. The back-fill leaves real gaps, so fill them with zero.
const dense = computed(() => {
	if (!props.series.length) return []
	const counts = new Map(props.series.map((point) => [point.month, point]))
	const out = []
	const [first, last] = [props.series[0].month, props.series[props.series.length - 1].month]
	for (let cursor = new Date(`${first}-01T00:00:00Z`); ; cursor.setUTCMonth(cursor.getUTCMonth() + 1)) {
		const key = cursor.toISOString().slice(0, 7)
		out.push(counts.get(key) || { month: key, n: 0, n_ai: 0 })
		if (key >= last) break
	}
	return out
})

// The API answers in months whatever is asked for — a decade of releases is
// ~150 rows — so changing granularity is a regroup here, not a round trip.
const buckets = computed(() => {
	const out = new Map()
	for (const point of dense.value) {
		const [year, month] = point.month.split('-')
		const key = grain.value === 'year' ? year
			: grain.value === 'quarter' ? `${year} Q${Math.floor((Number(month) - 1) / 3) + 1}`
			: point.month
		const bucket = out.get(key) || { label: key, n: 0, n_ai: 0 }
		bucket.n += point.n
		bucket.n_ai += point.n_ai
		out.set(key, bucket)
	}
	return [...out.values()]
})

const colors = computed(() => (scheme.value, palette()))

const data = computed(() => ({
	labels: buckets.value.map((b) => b.label),
	datasets: [
		{
			label: 'Product releases',
			data: buckets.value.map((b) => b.n),
			borderColor: colors.value.series[0],
			backgroundColor: colors.value.series[0],
			borderWidth: 2,
			pointRadius: 0,
			pointHoverRadius: 5,
			pointHoverBorderColor: colors.value.surface,
			pointHoverBorderWidth: 2,
			tension: 0.15,
		},
		...(props.showAi ? [{
			label: 'Shipping generative AI',
			data: buckets.value.map((b) => b.n_ai),
			borderColor: colors.value.series[1],
			backgroundColor: colors.value.series[1],
			borderWidth: 2,
			pointRadius: 0,
			pointHoverRadius: 5,
			pointHoverBorderColor: colors.value.surface,
			pointHoverBorderWidth: 2,
			tension: 0.15,
		}] : []),
	],
}))

const options = computed(() => ({
	...baseOptions(colors.value),
	scales: {
		x: axis(colors.value, {
			// Twenty-plus monthly ticks collide at this width; thinning them
			// keeps the axis readable without dropping any data.
			ticks: { maxRotation: 0, autoSkipPadding: 24 },
		}),
		y: axis(colors.value, {
			title: 'Releases',
			grid: true,
			ticks: { precision: 0 },
		}),
	},
}))
</script>

<template>
	<figure class="chart panel">
		<figcaption class="chart__head">
			<div>
				<h2>Releases over time</h2>
				<p class="small muted">Product releases by publication date.</p>
			</div>
			<div class="chart__controls" role="group" aria-label="Granularity">
				<button v-for="option in GRAINS" :key="option.key" type="button"
					class="grain" :class="{ 'grain--on': grain === option.key }"
					:aria-pressed="grain === option.key" @click="grain = option.key">
					{{ option.label }}
				</button>
			</div>
		</figcaption>

		<ul v-if="showAi" class="legend">
			<li v-for="(set, i) in data.datasets" :key="set.label">
				<span class="swatch" :style="{ background: colors.series[i] }" aria-hidden="true" />
				{{ set.label }}
			</li>
		</ul>

		<div class="chart__canvas">
			<div v-if="loading" class="skeleton chart__skeleton" />
			<p v-else-if="!buckets.length" class="empty muted">
				No releases to plot yet — the classifier has not produced any positives.
			</p>
			<Line v-else :data="data" :options="options" aria-label="Line chart of product releases over time" />
		</div>

		<details class="table">
			<summary class="small">Show the numbers</summary>
			<div class="table__scroll">
				<table>
					<thead>
						<tr>
							<th scope="col">Period</th>
							<th scope="col">Product releases</th>
							<th v-if="showAi" scope="col">Shipping generative AI</th>
						</tr>
					</thead>
					<tbody>
						<tr v-for="bucket in buckets" :key="bucket.label">
							<th scope="row">{{ bucket.label }}</th>
							<td class="tabular">{{ formatNumber(bucket.n) }}</td>
							<td v-if="showAi" class="tabular">{{ formatNumber(bucket.n_ai) }}</td>
						</tr>
					</tbody>
				</table>
			</div>
		</details>
	</figure>
</template>

<style scoped>
.chart {
	margin: 0;
	padding: var(--space-4) var(--space-5) var(--space-5);
}
.chart__head {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-3);
	align-items: flex-start;
	justify-content: space-between;
}
.chart__head p { margin: var(--space-1) 0 0; }
.chart__controls {
	display: flex;
	gap: 2px;
	padding: 2px;
	border: 1px solid var(--border);
	border-radius: var(--radius-2);
	background: var(--surface-sunk);
}
.grain {
	padding: 4px 11px;
	border: 0;
	border-radius: var(--radius-1);
	background: none;
	font-size: var(--text-sm);
	color: var(--ink-2);
	cursor: pointer;
}
.grain--on { background: var(--surface); color: var(--ink); font-weight: 600; box-shadow: var(--shadow); }

.legend {
	display: flex;
	gap: var(--space-4);
	margin: var(--space-3) 0 0;
	padding: 0;
	list-style: none;
	font-size: var(--text-sm);
	color: var(--ink-2);
}
.legend li { display: flex; align-items: center; gap: var(--space-2); }
.swatch { width: 10px; height: 10px; border-radius: 2px; display: inline-block; }

.chart__canvas { height: 300px; margin-top: var(--space-4); position: relative; }
.chart__skeleton { height: 100%; }
.empty { display: grid; place-items: center; height: 100%; text-align: center; }

.table { margin-top: var(--space-4); }
.table summary { cursor: pointer; color: var(--ink-2); }
.table__scroll { max-height: 280px; overflow: auto; margin-top: var(--space-3); }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
th, td { text-align: left; padding: 5px var(--space-3); border-bottom: 1px solid var(--border); }
td { text-align: right; }
thead th { position: sticky; top: 0; background: var(--surface); color: var(--ink-muted); font-weight: 600; }
thead th + th { text-align: right; }
</style>
