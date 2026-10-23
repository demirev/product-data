<script setup>
import { computed, inject, ref } from 'vue'

import Badge from './Badge.vue'
import { DATE, formatDate, formatNumber, humanize, setBlacklisted } from '../api.js'

const props = defineProps({
	company: { type: Object, required: true },
	hasAi: Boolean,
})
// The company has left the read model; the list should reload.
const emit = defineEmits(['blacklisted'])

const { canLabel } = inject('facets')
const saving = ref(false)
const failed = ref(null)

async function blacklist() {
	const reason = window.prompt(
		`Blacklist ${props.company.name}? All its releases leave the explorer.\nReason (optional):`, '')
	if (reason === null) return
	saving.value = true
	failed.value = null
	try {
		await setBlacklisted(props.company._id, true, reason)
		emit('blacklisted')
	} catch (failure) {
		failed.value = failure.message
	} finally {
		saving.value = false
	}
}

const PRN = 'https://www.prnewswire.com'

// Public / Private / Unknown, and Unknown is an answer rather than a gap: the
// sender's releases carried no concentrated ticker and no registry matched it.
const PUBLIC = {
	true: { label: 'Public', tone: 'yes' },
	false: { label: 'Private', tone: 'neutral' },
	null: { label: 'Public status unknown', tone: 'unknown' },
}
const status = computed(() => PUBLIC[String(props.company.is_public ?? null)])

const location = computed(() => {
	const c = props.company
	// Enriched location first; the modal dateline is the fallback the rollup
	// mined from the releases themselves.
	const parts = c.hq_city || c.country
		? [c.hq_city, c.region, c.country]
		: [(c.hq || {}).city, (c.hq || {}).region]
	return parts.filter(Boolean).join(', ')
})

const categories = computed(() =>
	Object.entries(props.company.product_categories || {})
		.sort((a, b) => b[1] - a[1])
		.slice(0, 4))

const revenue = computed(() => {
	const { revenue: value, revenue_unit: unit, revenue_year: year } = props.company
	if (!value) return null
	// A bare number is meaningless — Toyota reports in JPY — so the unit and the
	// year travel with it or it is not shown.
	const scaled = value >= 1e9 ? `${(value / 1e9).toFixed(1)}bn`
		: value >= 1e6 ? `${(value / 1e6).toFixed(1)}m`
		: formatNumber(value)
	return [scaled, unit, year && `(${year})`].filter(Boolean).join(' ')
})

const identifiers = computed(() => {
	const c = props.company
	return [
		c.wikidata_id && { label: 'Wikidata', href: `https://www.wikidata.org/wiki/${c.wikidata_id}` },
		c.cik && { label: 'SEC EDGAR', href: `https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=${c.cik}` },
		c.lei && { label: 'GLEIF', href: `https://search.gleif.org/#/record/${c.lei}` },
		c.ror_id && { label: 'ROR', href: c.ror_id },
	].filter(Boolean)
})

const facts = computed(() => {
	const c = props.company
	return [
		c.founded_year && { label: 'Founded', value: c.founded_year },
		c.employees && { label: 'Employees', value: formatNumber(c.employees) },
		revenue.value && { label: 'Revenue', value: revenue.value },
		c.sic_desc && { label: 'Industry', value: c.sic_desc },
		c.legal_form && { label: 'Legal form', value: c.legal_form },
		c.parent_name && { label: 'Parent', value: c.parent_name },
	].filter(Boolean)
})

const expanded = ref(false)
</script>

<template>
	<article class="card panel">
		<header class="card__head">
			<h3>
				<a :href="PRN + company._id" target="_blank" rel="noopener noreferrer">{{ company.name }}</a>
			</h3>
			<a v-if="company.domain" class="small card__domain"
				:href="`https://${company.domain}`" target="_blank" rel="noopener noreferrer">
				{{ company.domain }}
			</a>
		</header>

		<div class="card__badges">
			<Badge :tone="status.tone"
				:title="[company.is_public_method && `method: ${company.is_public_method}`,
					company.is_public_confidence && `confidence: ${company.is_public_confidence}`]
					.filter(Boolean).join(' · ') || undefined">
				{{ status.label }}
			</Badge>
			<Badge v-if="company.ticker" tone="yes">
				{{ company.ticker.exchange }}:{{ company.ticker.symbol }}
			</Badge>
			<Badge v-if="company.org_type">{{ humanize(company.org_type) }}</Badge>
		</div>
		<!-- The evidence, verbatim: `is_public_method` names the rule that
		     decided it, and humanizing "gleif_no_isin" into prose only makes it
		     look like broken English. -->
		<p v-if="company.is_public_method" class="small muted card__method">
			decided by <code>{{ company.is_public_method }}</code><template
				v-if="company.is_public_confidence">, {{ company.is_public_confidence }} confidence</template>
		</p>

		<p v-if="location" class="small muted card__location">{{ location }}</p>

		<dl class="counts">
			<div class="counts__primary">
				<dt>Product releases</dt>
				<dd class="tabular">{{ formatNumber(company.n_product_releases) }}</dd>
			</div>
			<div>
				<dt>Press releases</dt>
				<dd class="tabular">{{ formatNumber(company.n_releases) }}</dd>
			</div>
			<div v-if="hasAi">
				<dt>Shipping AI</dt>
				<dd class="tabular">{{ formatNumber(company.n_ai_releases) }}</dd>
			</div>
		</dl>

		<p class="small muted card__span">
			{{ formatDate(company.first_product_release, DATE) }} –
			{{ formatDate(company.last_product_release, DATE) }}
		</p>

		<ul v-if="categories.length" class="mix">
			<li v-for="[slug, n] in categories" :key="slug">
				<Badge>{{ humanize(slug) }} <span class="tabular muted">{{ n }}</span></Badge>
			</li>
		</ul>

		<dl v-if="facts.length" class="facts small">
			<template v-for="fact in facts" :key="fact.label">
				<dt>{{ fact.label }}</dt>
				<dd>{{ fact.value }}</dd>
			</template>
		</dl>

		<p v-if="company.about?.text" class="about small">
			<span :class="{ 'about--clamped': !expanded }">{{ company.about.text }}</span>
			<button class="linkish" type="button" :aria-expanded="expanded" @click="expanded = !expanded">
				{{ expanded ? 'Show less' : 'Show more' }}
			</button>
		</p>

		<footer class="card__foot">
			<RouterLink class="button" :to="{ path: '/products', query: { company: company._id } }">
				View releases
			</RouterLink>
			<button v-if="canLabel" class="button" type="button" :disabled="saving"
				@click="blacklist">Blacklist sender</button>
			<span v-if="failed" class="small" role="alert">{{ failed }}</span>
			<ul v-if="identifiers.length" class="ids small">
				<li v-for="id in identifiers" :key="id.label">
					<a :href="id.href" target="_blank" rel="noopener noreferrer">{{ id.label }}</a>
				</li>
			</ul>
		</footer>
	</article>
</template>

<style scoped>
.card {
	display: flex;
	flex-direction: column;
	gap: var(--space-3);
	padding: var(--space-4) var(--space-5) var(--space-5);
}
.card > * { margin: 0; }

.card__head { display: flex; flex-wrap: wrap; gap: var(--space-2); align-items: baseline; justify-content: space-between; }
.card__head h3 a { color: var(--ink); text-decoration: none; }
.card__head h3 a:hover { text-decoration: underline; text-decoration-color: var(--accent); }
.card__domain { word-break: break-all; }

.card__badges { display: flex; flex-wrap: wrap; gap: var(--space-2); }
.card__method, .card__location, .card__span { margin-top: calc(-1 * var(--space-2)); }
.card__method code { font-size: var(--text-xs); }

.counts {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-5);
	padding: var(--space-3) 0;
	border-top: 1px solid var(--border);
	border-bottom: 1px solid var(--border);
}
.counts dt { font-size: var(--text-xs); text-transform: uppercase; letter-spacing: 0.05em; color: var(--ink-muted); }
.counts dd { margin: 2px 0 0; font-size: var(--text-md); }
.counts__primary dd { font-size: var(--text-lg); font-weight: 600; }

.mix { display: flex; flex-wrap: wrap; gap: var(--space-2); margin: 0; padding: 0; list-style: none; }

.facts {
	display: grid;
	grid-template-columns: auto 1fr;
	gap: 3px var(--space-4);
}
.facts dt { color: var(--ink-muted); }
.facts dd { margin: 0; }

.about--clamped {
	display: -webkit-box;
	-webkit-line-clamp: 3;
	line-clamp: 3;
	-webkit-box-orient: vertical;
	overflow: hidden;
	color: var(--ink-2);
}
.linkish {
	display: block;
	margin-top: var(--space-1);
	border: 0;
	background: none;
	padding: 0;
	color: var(--accent);
	font-size: var(--text-sm);
	text-decoration: underline;
	cursor: pointer;
}

.card__foot {
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-3);
	align-items: center;
	justify-content: space-between;
	margin-top: auto;
	padding-top: var(--space-2);
}
.ids { display: flex; flex-wrap: wrap; gap: var(--space-3); margin: 0; padding: 0; list-style: none; }
</style>
