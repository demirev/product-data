// Chart.js registration and the chrome both charts share.
//
// Colors are read off the CSS custom properties rather than repeated here, so
// the palette has exactly one definition (see styles.css) and the dark steps
// are the ones chosen for the dark surface rather than an automatic flip.
import {
	BarController, BarElement, CategoryScale, Chart, Filler, Legend, LineController,
	LineElement, LinearScale, PointElement, Tooltip,
} from 'chart.js'
import { ref } from 'vue'

Chart.register(
	BarController, BarElement, CategoryScale, Filler, Legend, LineController,
	LineElement, LinearScale, PointElement, Tooltip,
)

const dark = window.matchMedia('(prefers-color-scheme: dark)')

// Chart.js bakes colors into its config, so a chart built under one scheme has
// to be rebuilt under the other. Components watch this.
export const scheme = ref(dark.matches ? 'dark' : 'light')
dark.addEventListener('change', (event) => {
	scheme.value = event.matches ? 'dark' : 'light'
})

export function palette() {
	const style = getComputedStyle(document.documentElement)
	const token = (name) => style.getPropertyValue(name).trim()
	return {
		series: [token('--series-1'), token('--series-2')],
		ink: token('--ink'),
		muted: token('--ink-muted'),
		grid: token('--grid'),
		axis: token('--axis'),
		surface: token('--surface'),
		border: token('--border-strong'),
	}
}

export function baseOptions(colors) {
	return {
		responsive: true,
		maintainAspectRatio: false,
		// Chart.js animates in canvas, where the CSS media query cannot reach it.
		animation: window.matchMedia('(prefers-reduced-motion: reduce)').matches
			? false
			: { duration: 400 },
		interaction: { mode: 'index', intersect: false },
		// The legend is drawn in the component, as text next to a swatch: it
		// stays readable when the canvas is narrow and it is real DOM, so the
		// series identity is never carried by color inside a canvas alone.
		plugins: {
			legend: { display: false },
			tooltip: {
				backgroundColor: colors.surface,
				titleColor: colors.ink,
				bodyColor: colors.ink,
				borderColor: colors.border,
				borderWidth: 1,
				padding: 10,
				cornerRadius: 8,
				displayColors: true,
				boxWidth: 8,
				boxHeight: 8,
				usePointStyle: true,
			},
		},
	}
}

// Hairline, recessive, and only along the axis the values are read against.
export function axis(colors, { title, grid = false, ticks = {} }) {
	return {
		border: { display: false },
		grid: { display: grid, color: colors.grid, drawTicks: false, lineWidth: 1 },
		ticks: { color: colors.muted, font: { size: 11 }, padding: 8, ...ticks },
		title: title
			? { display: true, text: title, color: colors.muted, font: { size: 11 } }
			: { display: false },
	}
}
