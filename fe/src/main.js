import { createApp } from 'vue'

import App from './App.vue'
import router from './router.js'
import './styles.css'

router.afterEach((to) => {
	document.title = `${to.meta.title || 'Explorer'} · Product Release Explorer`
})

createApp(App).use(router).mount('#app')
