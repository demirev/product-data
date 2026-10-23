<script setup>
import { inject, ref } from 'vue'

import { DATE, formatDate, formatNumber, setBlacklisted } from '../api.js'
import { useFetch } from '../composables/useFetch.js'

const PRN = 'https://www.prnewswire.com'

const { canLabel, loading: facetsLoading } = inject('facets')
const { data, loading, error, reload } = useFetch('/api/blacklist')

const removing = ref(null)
const failed = ref(null)

async function remove(item) {
	removing.value = item._id
	failed.value = null
	try {
		await setBlacklisted(item._id, false)
		await reload()
	} catch (failure) {
		failed.value = failure.message
	} finally {
		removing.value = null
	}
}
</script>

<template>
	<section class="panel list">
		<h2>Sender blacklist</h2>
		<p class="small muted intro">
			Releases from these senders are left out of products, companies and the overview,
			whatever the launch model called them. The classifier still scores them and their labels
			still train it. Removing a sender starts a refresh that brings its releases back.
		</p>

		<p v-if="!facetsLoading && !canLabel" class="muted">
			The blacklist is editable only with <code>EXPLORER_LABELER</code> set.
		</p>
		<div v-else-if="error" class="error" role="alert">
			<p>Could not load the blacklist: {{ error }}</p>
			<button class="button" type="button" @click="reload">Retry</button>
		</div>
		<div v-else-if="loading && !data" class="skeleton list__skeleton" />
		<p v-else-if="!data?.items.length" class="muted">No senders are blacklisted.</p>

		<div v-else class="table__scroll" :class="{ stale: loading }">
			<p v-if="failed" class="small" role="alert">{{ failed }}</p>
			<table>
				<thead>
					<tr>
						<th scope="col">Sender</th>
						<th scope="col">Reason</th>
						<th scope="col">Press releases</th>
						<th scope="col">Added</th>
						<th scope="col">By</th>
						<th scope="col"><span class="sr-only">Remove</span></th>
					</tr>
				</thead>
				<tbody>
					<tr v-for="item in data.items" :key="item._id">
						<th scope="row">
							<a :href="PRN + item._id" target="_blank" rel="noopener noreferrer">{{ item.name }}</a>
						</th>
						<td class="reason">{{ item.reason }}</td>
						<td class="tabular">{{ formatNumber(item.n_releases) }}</td>
						<td class="muted">{{ formatDate(item.added_at, DATE) }}</td>
						<td class="muted">{{ item.added_by }}<template v-if="item.source"> ({{ item.source }})</template></td>
						<td>
							<button class="button" type="button" :disabled="removing === item._id"
								@click="remove(item)">Remove</button>
						</td>
					</tr>
				</tbody>
			</table>
		</div>
	</section>
</template>

<style scoped>
.list { padding: var(--space-4) var(--space-5) var(--space-5); }
.intro { margin: var(--space-2) 0 var(--space-4); max-width: 78ch; }
.list__skeleton { height: 300px; }
.error { display: flex; flex-wrap: wrap; gap: var(--space-4); align-items: center; }
.stale { opacity: 0.5; }

.table__scroll { overflow-x: auto; }
table { width: 100%; border-collapse: collapse; font-size: var(--text-sm); }
th, td { text-align: left; padding: 6px var(--space-3); border-bottom: 1px solid var(--border); white-space: nowrap; }
thead th { color: var(--ink-muted); font-weight: 600; }
tbody th { font-weight: 500; white-space: normal; }
td.reason { white-space: normal; color: var(--ink-2); }
tbody tr:last-child th, tbody tr:last-child td { border-bottom: 0; }
code { font-size: var(--text-xs); }
</style>
