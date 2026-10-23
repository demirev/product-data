<script setup>
import { ref, watch } from 'vue'

import { fetchJson } from '../api.js'

// One panel, described by data, for both tabs: the two filter sets differ in
// which fields they have, not in how a field behaves. `fields` is a list of
// descriptors — {kind, key, label, ...} — and the view keeps the values, since
// they live in the URL.
const props = defineProps({
	fields: { type: Array, required: true },
	values: { type: Object, required: true },
})
const emit = defineEmits(['update', 'updateSoon', 'toggle'])

// Kinds whose input is committed on a debounce rather than on change.
const DEBOUNCED = ['text', 'company', 'slider']

const open = ref(false)
const drafts = ref({})
const committed = {}
const matches = ref([])
const searching = ref(false)

// Keep the debounced inputs' local text in step with the URL — but only when
// the URL moved on its own (a back button, a removed chip). If it merely caught
// up with what was typed, leaving the draft alone is what stops the cursor from
// jumping mid-word.
watch(() => props.values, (values) => {
	for (const field of props.fields) {
		if (!DEBOUNCED.includes(field.kind)) continue
		const incoming = values[field.key] ?? ''
		if (incoming !== (committed[field.key] ?? '')) {
			drafts.value[field.key] = incoming
			committed[field.key] = incoming
		}
	}
}, { immediate: true, deep: true })

function commit(key, value, soon) {
	committed[key] = value ?? ''
	emit(soon ? 'updateSoon' : 'update', { [key]: value })
}

let controller = null

async function searchCompanies(term) {
	drafts.value.company = term
	controller?.abort()
	if (!term.trim()) {
		matches.value = []
		return
	}
	controller = new AbortController()
	searching.value = true
	try {
		const page = await fetchJson('/api/companies',
			{ q: term, per_page: 8, sort: '-n_product_releases' }, controller.signal)
		matches.value = page.items
	} catch (error) {
		if (error.name !== 'AbortError') matches.value = []
	} finally {
		searching.value = false
	}
}

function pickCompany(company) {
	matches.value = []
	drafts.value.company = company ? company.name : ''
	commit('company', company ? company._id : null, false)
}
</script>

<template>
	<aside class="filters panel" :class="{ 'filters--open': open }">
		<div class="filters__head">
			<h2>Filters</h2>
			<button class="button filters__toggle" type="button"
				:aria-expanded="open" aria-controls="filter-fields" @click="open = !open">
				{{ open ? 'Hide' : 'Show' }} filters
			</button>
		</div>

		<div id="filter-fields" class="filters__body">
			<div v-for="field in fields" :key="field.key || field.label" class="filters__field">

				<template v-if="field.kind === 'text'">
					<label class="field-label" :for="`f-${field.key}`">{{ field.label }}</label>
					<input :id="`f-${field.key}`" class="input" type="search"
						:placeholder="field.placeholder"
						:value="drafts[field.key] ?? ''"
						@input="commit(field.key, $event.target.value, true)" />
				</template>

				<template v-else-if="field.kind === 'company'">
					<label class="field-label" for="f-company">{{ field.label }}</label>
					<div class="combo">
						<input id="f-company" class="input" type="search" autocomplete="off"
							role="combobox" aria-expanded="false" aria-controls="company-matches"
							:placeholder="field.placeholder"
							:value="drafts.company ?? ''"
							@input="searchCompanies($event.target.value)" />
						<ul v-if="matches.length" id="company-matches" class="combo__list">
							<li v-for="company in matches" :key="company._id">
								<button type="button" @click="pickCompany(company)">
									<span>{{ company.name }}</span>
									<span class="muted tabular">{{ company.n_product_releases }}</span>
								</button>
							</li>
						</ul>
						<p v-else-if="searching" class="small muted">Searching…</p>
					</div>
					<button v-if="values.company" class="linkish small" type="button"
						@click="pickCompany(null)">Clear company</button>
				</template>

				<template v-else-if="field.kind === 'daterange'">
					<span class="field-label">{{ field.label }}</span>
					<div class="row">
						<label class="sr-only" for="f-from">From</label>
						<input id="f-from" class="input" type="date" :min="field.min" :max="field.max"
							:value="values.from ?? ''" @change="commit('from', $event.target.value, false)" />
						<span class="muted small">to</span>
						<label class="sr-only" for="f-to">To</label>
						<input id="f-to" class="input" type="date" :min="field.min" :max="field.max"
							:value="values.to ?? ''" @change="commit('to', $event.target.value, false)" />
					</div>
				</template>

				<template v-else-if="field.kind === 'multi'">
					<fieldset class="group">
						<legend class="field-label">{{ field.label }}</legend>
						<div class="group__scroll">
							<label v-for="option in field.options" :key="option.value" class="check">
								<input type="checkbox"
									:checked="values[field.key].includes(option.value)"
									@change="emit('toggle', field.key, option.value)" />
								<span>{{ option.label }}</span>
							</label>
						</div>
					</fieldset>
				</template>

				<template v-else-if="field.kind === 'choice'">
					<fieldset class="group">
						<legend class="field-label">{{ field.label }}</legend>
						<label v-for="option in field.options" :key="option.value ?? 'any'" class="check">
							<input type="radio" :name="field.key"
								:checked="(values[field.key] ?? null) === option.value"
								@change="commit(field.key, option.value, false)" />
							<span>{{ option.label }}</span>
						</label>
					</fieldset>
				</template>

				<template v-else-if="field.kind === 'bool'">
					<label class="check check--standalone">
						<input type="checkbox" :checked="values[field.key] === 'true'"
							@change="commit(field.key, $event.target.checked ? 'true' : null, false)" />
						<span>{{ field.label }}</span>
					</label>
				</template>

				<template v-else-if="field.kind === 'slider'">
					<label class="field-label" :for="`f-${field.key}`">
						{{ field.label }}
						<span class="tabular">{{ Math.round((drafts[field.key] || 0) * 100) }}%</span>
					</label>
					<input :id="`f-${field.key}`" type="range" class="slider"
						min="0" max="0.99" step="0.01"
						:value="drafts[field.key] || 0"
						@input="commit(field.key, $event.target.value === '0' ? null : $event.target.value, true)" />
				</template>

				<template v-else-if="field.kind === 'numrange'">
					<span class="field-label">{{ field.label }}</span>
					<div class="row">
						<label class="sr-only" :for="`f-${field.from}`">Minimum</label>
						<input :id="`f-${field.from}`" class="input" type="number" inputmode="numeric"
							:placeholder="field.minPlaceholder" :min="field.min" :max="field.max"
							:value="values[field.from] ?? ''"
							@change="commit(field.from, $event.target.value, false)" />
						<span class="muted small">to</span>
						<label class="sr-only" :for="`f-${field.to}`">Maximum</label>
						<input :id="`f-${field.to}`" class="input" type="number" inputmode="numeric"
							:placeholder="field.maxPlaceholder" :min="field.min" :max="field.max"
							:value="values[field.to] ?? ''"
							@change="commit(field.to, $event.target.value, false)" />
					</div>
				</template>

			</div>
		</div>
	</aside>
</template>

<style scoped>
.filters {
	padding: var(--space-4) var(--space-5) var(--space-5);
	align-self: start;
}
.filters__head {
	display: flex;
	align-items: center;
	justify-content: space-between;
	gap: var(--space-3);
}
.filters__toggle { display: none; }
.filters__field + .filters__field {
	margin-top: var(--space-4);
	padding-top: var(--space-4);
	border-top: 1px solid var(--border);
}
.filters__body > .filters__field:first-child { margin-top: var(--space-4); }

.row {
	display: flex;
	align-items: center;
	gap: var(--space-2);
}
.row .input { min-width: 0; }

.group { border: 0; margin: 0; padding: 0; }
.group__scroll { max-height: 190px; overflow-y: auto; padding-right: var(--space-2); }

.check {
	display: flex;
	align-items: center;
	gap: var(--space-2);
	padding: 3px 0;
	font-size: var(--text-sm);
	cursor: pointer;
}
.check input { accent-color: var(--accent); margin: 0; }
.check--standalone { font-weight: 500; }

.slider { width: 100%; accent-color: var(--accent); }

.combo { position: relative; }
.combo__list {
	position: absolute;
	z-index: 5;
	left: 0; right: 0;
	margin: var(--space-1) 0 0;
	padding: var(--space-1);
	list-style: none;
	max-height: 240px;
	overflow-y: auto;
	background: var(--surface);
	border: 1px solid var(--border-strong);
	border-radius: var(--radius-2);
	box-shadow: 0 6px 20px rgba(11, 11, 11, 0.14);
}
.combo__list button {
	display: flex;
	width: 100%;
	gap: var(--space-3);
	justify-content: space-between;
	padding: var(--space-2);
	border: 0;
	border-radius: var(--radius-1);
	background: none;
	font-size: var(--text-sm);
	text-align: left;
	cursor: pointer;
}
.combo__list button:hover { background: var(--surface-sunk); }

.linkish {
	margin-top: var(--space-2);
	border: 0;
	background: none;
	padding: 0;
	color: var(--accent);
	text-decoration: underline;
	cursor: pointer;
}

@media (max-width: 899px) {
	.filters__toggle { display: inline-flex; }
	.filters__body { display: none; }
	.filters--open .filters__body { display: block; }
}
</style>
