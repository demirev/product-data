<script setup>
import { computed, provide } from 'vue'

import { useFetch } from './composables/useFetch.js'
import { formatDate, formatNumber } from './api.js'

// The facet lists and the served model versions are the same for every view and
// change only when the read model is rebuilt, so they are fetched once here and
// handed down rather than re-requested per tab.
const { data: filters, loading, error, reload } = useFetch('/api/filters')

provide('facets', {
	filters,
	loading,
	// Every AI affordance in the app is conditional on this: with no `ai` run
	// served there is no badge, no filter and no tile, because `is_ai` is null
	// rather than false and a "not AI" answer would be an invention.
	hasAi: computed(() => Boolean(filters.value?.models?.ai)),
	// Set by the server's EXPLORER_LABELER; the API refuses the write without it.
	canLabel: computed(() => Boolean(filters.value?.labeling)),
})
</script>

<template>
	<a class="skip" href="#main">Skip to content</a>

	<header class="masthead">
		<div class="masthead__inner">
			<div>
				<h1>Product Release Explorer</h1>
				<p class="masthead__sub small">
					New product and service launches, extracted from PR Newswire press releases
					by a classifier and joined to company records.
				</p>
			</div>
			<nav class="tabs" aria-label="Sections">
				<RouterLink to="/">Overview</RouterLink>
				<RouterLink to="/products">Products</RouterLink>
				<RouterLink to="/companies">Companies</RouterLink>
				<RouterLink v-if="filters?.labeling" to="/blacklist">Blacklist</RouterLink>
			</nav>
		</div>
	</header>

	<main id="main">
		<p v-if="error" class="masthead__error panel" role="alert">
			Could not load the dataset description: {{ error }}
			<button class="button" type="button" @click="reload">Retry</button>
		</p>
		<RouterView />
	</main>

	<footer class="colophon small muted">
		<span v-if="filters?.built_at">
			{{ formatNumber(filters.n_releases) }} product releases from
			{{ formatNumber(filters.n_companies) }} companies · read model built
			{{ formatDate(filters.built_at) }}
		</span>
		<span v-if="filters?.models?.launch">
			· served run <code>{{ filters.models.launch.version }}</code>
			<template v-if="filters.models.ai"> and <code>{{ filters.models.ai.version }}</code></template>
		</span>
	</footer>
</template>

<style scoped>
.skip {
	position: absolute;
	left: -999px;
}
.skip:focus {
	left: var(--space-4);
	top: var(--space-4);
	z-index: 10;
	padding: var(--space-2) var(--space-4);
	background: var(--surface);
	border: 1px solid var(--border-strong);
	border-radius: var(--radius-2);
}

.masthead {
	border-bottom: 1px solid var(--border);
	background: var(--surface);
}
.masthead__inner {
	max-width: 1240px;
	margin: 0 auto;
	padding: var(--space-5) var(--space-5) 0;
	display: flex;
	flex-wrap: wrap;
	gap: var(--space-4);
	justify-content: space-between;
	align-items: flex-start;
}
.masthead__sub {
	margin: var(--space-2) 0 0;
	max-width: 62ch;
	color: var(--ink-2);
}
.masthead__error {
	max-width: 1240px;
	margin: var(--space-5) auto 0;
	padding: var(--space-4);
	display: flex;
	gap: var(--space-4);
	align-items: center;
	justify-content: space-between;
}

.tabs {
	display: flex;
	gap: var(--space-1);
	margin-left: auto;
}
.tabs a {
	padding: var(--space-3) var(--space-4);
	border-bottom: 2px solid transparent;
	color: var(--ink-2);
	font-weight: 500;
	text-decoration: none;
}
.tabs a:hover { color: var(--ink); }
.tabs a.router-link-active {
	color: var(--ink);
	border-bottom-color: var(--accent);
}

main {
	max-width: 1240px;
	margin: 0 auto;
	padding: var(--space-5);
}

.colophon {
	max-width: 1240px;
	margin: 0 auto;
	padding: 0 var(--space-5) var(--space-6);
}
.colophon code { font-size: var(--text-xs); }

@media (max-width: 640px) {
	.masthead__inner { padding: var(--space-4) var(--space-4) 0; }
	main, .colophon { padding-left: var(--space-4); padding-right: var(--space-4); }
	.tabs a { padding: var(--space-3) var(--space-2); }
}
</style>
