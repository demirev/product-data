import { createRouter, createWebHistory } from 'vue-router'

import OverviewView from './views/OverviewView.vue'
import ProductsView from './views/ProductsView.vue'
import CompaniesView from './views/CompaniesView.vue'
import BlacklistView from './views/BlacklistView.vue'

export default createRouter({
	history: createWebHistory(),
	routes: [
		{ path: '/', name: 'overview', component: OverviewView, meta: { title: 'Overview' } },
		{ path: '/products', name: 'products', component: ProductsView, meta: { title: 'Products' } },
		{ path: '/companies', name: 'companies', component: CompaniesView, meta: { title: 'Companies' } },
		{ path: '/blacklist', name: 'blacklist', component: BlacklistView, meta: { title: 'Blacklist' } },
		// History mode with a server that falls every unmatched path through to
		// index.html: an unknown path lands here rather than on a blank page.
		{ path: '/:pathMatch(.*)*', redirect: '/' },
	],
	scrollBehavior(to, from) {
		// Paging and filtering keep their place in the URL, so scrolling to the
		// top on every query change would fight the reader. Only a tab switch
		// is a new page.
		return to.path === from.path ? false : { top: 0 }
	},
})
