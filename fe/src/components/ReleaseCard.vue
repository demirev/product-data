<script setup>
import { computed, inject, ref, watch } from 'vue'

import Badge from './Badge.vue'
import { DATE, formatDate, humanize, setBlacklisted } from '../api.js'

const props = defineProps({
	release: { type: Object, required: true },
})
// The sender's releases have left the read model; the list should reload.
const emit = defineEmits(['blacklisted'])

const dateline = computed(() => {
	const { city, region } = props.release.dateline || {}
	return [city, region].filter(Boolean).join(', ')
})

// The classifier's own probability for the positive class. Shown as a meter
// because the useful reading is "how sure, roughly" — the exact figure is in
// the CSV, and four significant digits on a card invite false precision.
const confidence = computed(() => Math.round((props.release.confidence || 0) * 100))

const { canLabel } = inject('facets')

// `launch_class` of the labeler's three negative decisions.
const FALSE_POSITIVES = [
	{ value: 3, label: 'Feature release' },
	{ value: 2, label: 'Product info, no launch' },
	{ value: 0, label: 'Irrelevant' },
]

const label = ref(props.release.label)
watch(() => props.release, (release) => { label.value = release.label })
const saving = ref(false)
const failed = ref(null)

const marked = computed(() => {
	const value = label.value?.launch_class
	return FALSE_POSITIVES.some((option) => option.value === value) ? value : ''
})

// `launchClass: null` clears the label.
async function mark(launchClass, select) {
	saving.value = true
	failed.value = null
	try {
		const response = await fetch('/api/labels', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ article_id: props.release._id, launch_class: launchClass }),
		})
		if (!response.ok) throw new Error(`Could not save (${response.status})`)
		label.value = (await response.json()).label
	} catch (failure) {
		failed.value = failure.message
		if (select) select.value = marked.value  // the control moved, the label did not
	} finally {
		saving.value = false
	}
}

async function blacklist() {
	const reason = window.prompt(
		`Blacklist ${props.release.company_name}? All its releases leave the explorer.\nReason (optional):`, '')
	if (reason === null) return
	saving.value = true
	failed.value = null
	try {
		await setBlacklisted(props.release.company_id, true, reason)
		emit('blacklisted')
	} catch (failure) {
		failed.value = failure.message
	} finally {
		saving.value = false
	}
}
</script>

<template>
	<article class="card panel" :class="{ 'card--rejected': marked !== '' }">
		<h3 class="card__title">
			<a :href="release.url" target="_blank" rel="noopener noreferrer">{{ release.title }}</a>
		</h3>

		<p class="card__meta small">
			<RouterLink v-if="release.company_id" class="card__company"
				:to="{ path: '/companies', query: { company: release.company_id } }">
				{{ release.company_name }}
			</RouterLink>
			<span v-else class="muted">Unknown sender</span>
			<time :datetime="release.published_at">{{ formatDate(release.published_at, DATE) }}</time>
			<span v-if="dateline" class="muted">{{ dateline }}</span>
		</p>

		<p class="card__summary">{{ release.summary }}</p>

		<div class="card__foot">
			<Badge v-if="release.category">{{ humanize(release.category) }}</Badge>
			<Badge v-if="release.is_ai" tone="ai">Generative AI</Badge>
			<Badge v-for="ticker in release.tickers" :key="`${ticker.exchange}:${ticker.symbol}`">
				{{ ticker.exchange }}:{{ ticker.symbol }}
			</Badge>

			<div class="meter" role="img"
				:aria-label="`Launch confidence ${confidence} percent`"
				:title="`Launch confidence ${confidence}%`">
				<span class="meter__track"><span class="meter__fill" :style="{ width: `${confidence}%` }" /></span>
				<span class="meter__value tabular">{{ confidence }}%</span>
			</div>
		</div>

		<div v-if="canLabel" class="card__label">
			<label class="sr-only" :for="`label-${release._id}`">Mark as a false positive</label>
			<select :id="`label-${release._id}`" class="input" :value="marked" :disabled="saving"
				@change="mark(Number($event.target.value), $event.target)">
				<option value="" disabled>False positive…</option>
				<option v-for="option in FALSE_POSITIVES" :key="option.value" :value="option.value">
					{{ option.label }}
				</option>
			</select>
			<button v-if="label?.source === 'explorer'" class="button" type="button"
				:disabled="saving" @click="mark(null)">Undo</button>
			<button v-if="release.company_id" class="button" type="button"
				:disabled="saving" @click="blacklist">Blacklist sender</button>
			<span v-if="failed" class="small" role="alert">{{ failed }}</span>
		</div>
	</article>
</template>

<style scoped>
.card {
	display: flex;
	flex-direction: column;
	padding: var(--space-4) var(--space-5) var(--space-5);
}
.card__title { font-size: var(--text-md); }
.card__title a { color: var(--ink); text-decoration: none; }
.card__title a:hover { text-decoration: underline; text-decoration-color: var(--accent); }

.card__meta {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-2);
	align-items: baseline;
	margin: var(--space-2) 0 0;
}
/* The separator belongs to the item after it, so a wrap never leaves a lone
   middot dangling at the end of a line. */
.card__meta > * + *::before {
	content: '· ';
	color: var(--ink-muted);
}
.card__company { font-weight: 500; }

.card__summary {
	margin: var(--space-3) 0 var(--space-4);
	color: var(--ink-2);
	font-size: var(--text-sm);
	/* Four lines, so a grid of cards keeps a common rhythm whatever the lede. */
	display: -webkit-box;
	-webkit-line-clamp: 4;
	line-clamp: 4;
	-webkit-box-orient: vertical;
	overflow: hidden;
}

.card__foot {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-2);
	align-items: center;
	margin-top: auto;
	padding-top: var(--space-2);
}

.card__label {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-2);
	align-items: center;
	margin-top: var(--space-3);
	padding-top: var(--space-3);
	border-top: 1px solid var(--border);
}
.card__label .input { flex: 1 1 0; min-width: 0; }
.card__label [role="alert"] { flex-basis: 100%; }
.card--rejected > :not(.card__label) { opacity: 0.45; }

.meter {
	display: flex;
	align-items: center;
	gap: var(--space-2);
	margin-left: auto;
}
.meter__track {
	display: block;
	width: 52px;
	height: 4px;
	border-radius: 999px;
	background: var(--grid);
	overflow: hidden;
}
.meter__fill {
	display: block;
	height: 100%;
	border-radius: 999px;
	background: var(--accent);
}
.meter__value { font-size: var(--text-xs); color: var(--ink-muted); }
</style>
