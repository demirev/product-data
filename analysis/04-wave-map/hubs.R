library(data.table)
library(ggplot2)
pdf(NULL)

# Over/under-representation of tech hubs in each technology wave, by years since the wave took off.
#
# A region's ratio is its share of a wave's companies over the share expected from other product releases: each
# company counts as the region's share of the placed companies with a product release in the same calendar year
# that does not match the wave's technology family (a series, its broad parent and narrower children, as in
# build.py), since hubs' shares of these companies drift over the years. Year 0 is the first month of the wave's half-peak window in
# 03-tech-waves/waves.txt. Regions are sets of GeoNames counties (admin2); New York City's own entry has none, so
# it is listed by id, and Connecticut is coded by planning region.
#
# Outputs, per region:
#   <region>.png           cumulative: the region's share of the wave's companies so far. A line starts once the
#                          wave has 30 placed companies. The dashed average wave is the month effects of a
#                          least-squares fit of log ratio on wave and month since take-off (wave effects summing to
#                          zero), drawn where at least five waves have a ratio.
#   <region>_entrants.png  the region's share of each year's new entrants, with 95% Wilson intervals; years with
#                          fewer than 20 entrants are dropped. The average wave is the year effects of a Poisson fit
#                          of region entrants on wave and year, offset by expected entrants, with 95% intervals.
# and hubs.png, every region's average wave under both measures; hubs_cumulative.png, the cumulative measure from year 0.
#
#   Rscript hubs.R ../03-tech-waves/companies_<launch version>.csv ../03-tech-waves/releases_<launch version>.csv

args <- commandArgs(trailingOnly = TRUE)
companies_file <- normalizePath(args[1])
releases_file <- normalizePath(args[2])
setwd(dirname(normalizePath(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)))))

labels <- c(
	mobile = "Mobile apps", cloud = "Cloud", saas = "SaaS", big_data = "Big data", iot = "Internet of things", printing_3d = "3D printing",
	wearables = "Wearables", xr = "VR / AR", metaverse = "Metaverse", crypto = "Crypto",
	genai = "Generative AI (excl. agents)", agentic = "Agentic AI", ai = "Any AI"
)
parent <- c(cloud = "cloud_broad", saas = "cloud", iot = "iot_broad", wearables = "wearables_broad", crypto = "crypto_broad",
	ai = "ai_broad", genai_broad = "ai_broad", genai = "genai_broad", agentic = "ai")
regions <- list(
	list(key = "silicon_valley", name = "Silicon Valley", what = "Santa Clara and San Mateo counties", counties = c("CA.085", "CA.081")),
	list(key = "silicon_valley_sf", name = "Silicon Valley and San Francisco", what = "Santa Clara, San Mateo and San Francisco counties",
		counties = c("CA.085", "CA.081", "CA.075")),
	list(key = "new_york", name = "New York area", ids = "5128581",
		what = "New York City, Long Island, lower Hudson Valley, northern and central New Jersey, Fairfield County (Conn.)",
		counties = c(paste0("NY.", c("005", "047", "061", "081", "085", "059", "103", "119", "087", "079")),
			paste0("NJ.", c("003", "013", "017", "019", "021", "023", "025", "027", "029", "031", "035", "037", "039")), "CT.190", "CT.120")),
	list(key = "boston", name = "Boston area", what = "Suffolk, Middlesex, Norfolk, Essex and Plymouth counties",
		counties = paste0("MA.", c("025", "017", "021", "009", "023"))),
	list(key = "austin", name = "Austin area", what = "Travis, Williamson, Hays, Bastrop and Caldwell counties",
		counties = paste0("TX.", c("453", "491", "209", "021", "055"))),
	list(key = "los_angeles", name = "Los Angeles area", what = "Los Angeles and Orange counties", counties = c("CA.037", "CA.059"))
)

geo <- fread("geo.csv", colClasses = c(geonameid = "character"))[!org_type %in% c("law_firm", "research_publisher", "stock_commentary")]
us <- fread("../../data/geonames/cities500.txt", sep = "\t", quote = "", header = FALSE, select = c(1, 9, 11, 12),
	col.names = c("geonameid", "cc", "admin1", "admin2"), colClasses = "character")[cc == "US"]
firms <- fread(companies_file)[geo[, .(uploader_link, geonameid)], on = "uploader_link", nomatch = NULL]
releases <- fread(releases_file, colClasses = c(year = "character"))[geo[, .(uploader_link, geonameid)], on = "uploader_link", nomatch = NULL]
# Sender-years whose product releases all match the family
releases[, full := n == n[family == "_total"], by = .(uploader_link, year)]
firms[, fam := vapply(series, function(s) { while (s %in% names(parent)) s <- parent[[s]]; s }, "")]
firms[, `:=`(month = pmax(first_month, "2010-01"), cal = substr(pmax(first_month, "2010-01"), 1, 4))]
waves <- fread(text = grep("^ *[a-z_0-9]+ +(all|launch) ", readLines("../03-tech-waves/waves.txt"), value = TRUE),
	header = FALSE, col.names = c("series", "group", "peak_month", "peak", "half_from", "half_to", "last", "matches"))[group == "all"]
firms[waves, rel := 12 * (as.integer(substr(month, 1, 4)) - as.integer(substr(i.half_from, 1, 4)))
	+ as.integer(substr(month, 6, 7)) - as.integer(substr(i.half_from, 6, 7)), on = "series"]
order <- waves[series %in% names(labels)][order(half_from)]
levels <- paste0(labels[order$series], "  (", order$half_from, ")")
months <- format(seq(as.Date("2010-01-01"), as.Date(paste0(max(firms$month), "-01")), by = "month"), "%Y-%m")
y_ratio <- scale_y_continuous(trans = "log2", breaks = c(0.25, 0.5, 1, 2, 4), labels = function(x) paste0(x, "×"),
	limits = c(1 / 6, 6), oob = scales::squish)
theme_panels <- theme_minimal(base_size = 11) +
	theme(panel.grid.minor = element_blank(), strip.text = element_text(hjust = 0, face = "bold"), plot.title.position = "plot")
averages <- list()

for (r in regions) {
	ids <- c(us[paste(admin1, admin2, sep = ".") %in% r$counties, geonameid], r$ids)
	firms[, inr := geonameid %in% ids]
	releases[, inr := geonameid %in% ids]
	# Per family and calendar year: the region's share of companies with a product release outside the family that year
	base <- CJ(fam = unique(firms$fam), cal = unique(releases$year))
	base[releases[family == "_total", .(n = .N, n_r = sum(inr)), by = year], `:=`(n = i.n, n_r = i.n_r), on = .(cal = year)]
	base[releases[family != "_total" & full, .(n = .N, n_r = sum(inr)), by = .(family, year)],
		`:=`(n = n - i.n, n_r = n_r - i.n_r), on = .(fam = family, cal = year)]
	firms[base, b := i.n_r / i.n, on = .(fam, cal)]
	ylab <- paste0(r$name, "'s share of the wave's companies, relative to its share\nof companies with other product releases in the same years")

	# Cumulative
	d <- firms[series %in% names(labels), .(n = .N, n_r = sum(inr), x = sum(b)), by = .(series, month)]
	d <- d[CJ(series = unique(d$series), month = months), on = .(series, month)]
	d[is.na(n), `:=`(n = 0L, n_r = 0L, x = 0)]
	setkey(d, series, month)
	d[, `:=`(cum = cumsum(n), cum_r = cumsum(n_r), cum_x = cumsum(x)), by = series]
	d[waves, half_from := i.half_from, on = "series"]
	d[, `:=`(ratio = cum_r / cum_x,
		years = (12 * (as.integer(substr(month, 1, 4)) - as.integer(substr(half_from, 1, 4)))
			+ as.integer(substr(month, 6, 7)) - as.integer(substr(half_from, 6, 7))) / 12)]
	d <- d[cum >= 30 & years >= -4 & years <= 10]
	fit <- lm(log(ratio) ~ 0 + factor(years) + series, d[ratio > 0], contrasts = list(series = "contr.sum"))
	avg <- d[ratio > 0, .(waves = .N), by = years][, ratio := exp(coef(fit)[paste0("factor(years)", years)])][waves >= 5]
	averages[[length(averages) + 1]] <- avg[, .(region = r$name, measure = "Cumulative share of the wave's companies", years, ratio)]
	d[, label := factor(paste0(labels[series], "  (", half_from, ")"), levels)]

	p <- ggplot(d, aes(years, ratio)) +
		geom_hline(yintercept = 1, colour = "grey55", linewidth = 0.4) +
		geom_vline(xintercept = 0, colour = "grey80", linewidth = 0.4) +
		geom_line(data = d[, !"label"], aes(group = series), colour = "grey82", linewidth = 0.35) +
		geom_line(data = avg, colour = "grey15", linewidth = 0.6, linetype = "22") +
		geom_line(colour = "#2a78d6", linewidth = 0.9) +
		facet_wrap(~label, ncol = 4) + y_ratio + scale_x_continuous(breaks = seq(-4, 10, 2)) +
		labs(x = "Years since the wave took off (first month of its half-peak window, in brackets)", y = ylab,
			title = paste0(r$name, ": over-representation as each technology wave develops"),
			subtitle = paste0(r$what, "\nCumulative from 2010; grey: other waves; dashed: average wave")) +
		theme_panels
	ggsave(paste0(r$key, ".png"), p, width = 12, height = 10, dpi = 150)

	# New entrants per year since take-off
	e <- firms[series %in% names(labels) & rel >= -48 & rel < 120, .(n = .N, n_r = sum(inr), x = sum(b)), by = .(series, year = rel %/% 12)][n >= 20]
	e[, `:=`(p = n_r / n, z = 1.96)]
	e[, `:=`(ratio = n_r / x,
		lo = ((p + z^2 / (2 * n)) - z * sqrt(p * (1 - p) / n + z^2 / (4 * n^2))) / (1 + z^2 / n) * n / x,
		hi = ((p + z^2 / (2 * n)) + z * sqrt(p * (1 - p) / n + z^2 / (4 * n^2))) / (1 + z^2 / n) * n / x)]
	pois <- glm(n_r ~ 0 + factor(year) + series + offset(log(x)), poisson, e, contrasts = list(series = "contr.sum"))
	ci <- exp(cbind(coef(pois), confint.default(pois)))
	eavg <- e[, .(waves = .N), by = year][waves >= 5]
	eavg[, c("ratio", "lo", "hi") := as.data.table(ci[paste0("factor(year)", year), , drop = FALSE])]
	averages[[length(averages) + 1]] <- eavg[, .(region = r$name, measure = "Share of each year's new entrants", years = year + 0.5, ratio, lo, hi)]
	e[waves, half_from := i.half_from, on = "series"]
	e[, label := factor(paste0(labels[series], "  (", half_from, ")"), levels)]

	p <- ggplot(e, aes(year + 0.5, ratio)) +
		geom_hline(yintercept = 1, colour = "grey55", linewidth = 0.4) +
		geom_vline(xintercept = 0, colour = "grey80", linewidth = 0.4) +
		geom_line(data = e[, !"label"], aes(group = series), colour = "grey82", linewidth = 0.35) +
		geom_ribbon(data = eavg, aes(ymin = lo, ymax = hi), fill = "grey15", alpha = 0.1) +
		geom_line(data = eavg, colour = "grey15", linewidth = 0.6, linetype = "22") +
		geom_errorbar(aes(ymin = lo, ymax = hi), colour = "#2a78d6", width = 0, linewidth = 0.5, alpha = 0.6) +
		geom_line(colour = "#2a78d6", linewidth = 0.9) + geom_point(colour = "#2a78d6", size = 1.6) +
		facet_wrap(~label, ncol = 4) + y_ratio + scale_x_continuous(breaks = seq(-4, 10, 2), limits = c(-4, 10)) +
		labs(x = "Years since the wave took off (first month of its half-peak window, in brackets)", y = ylab,
			title = paste0(r$name, ": share of each year's new entrants, as each technology wave develops"),
			subtitle = paste0(r$what, "\nPoints: years with 20+ new companies, 95% intervals; grey: other waves; dashed: average wave, 95% band")) +
		theme_panels
	ggsave(paste0(r$key, "_entrants.png"), p, width = 12, height = 10, dpi = 150)
}

a <- rbindlist(averages, fill = TRUE)
a[, region := factor(region, vapply(regions, `[[`, "", "name"))]
ggplot(a, aes(years, ratio, colour = region, fill = region)) +
	geom_hline(yintercept = 1, colour = "grey55", linewidth = 0.4) +
	geom_vline(xintercept = 0, colour = "grey80", linewidth = 0.4) +
	geom_ribbon(aes(ymin = lo, ymax = hi), alpha = 0.12, colour = NA) +
	geom_line(linewidth = 0.9) +
	facet_wrap(~measure) +
	scale_colour_manual(values = c("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300"), aesthetics = c("colour", "fill")) +
	scale_y_continuous(trans = "log2", breaks = c(0.5, 2 / 3, 0.8, 1, 1.25, 1.5, 2), labels = function(x) paste0(signif(x, 2), "×")) +
	scale_x_continuous(breaks = seq(-4, 10, 2)) +
	labs(x = "Years since the wave took off", y = "Region's share of the average wave's companies, relative to\nits share of companies with other product releases, same years",
		colour = NULL, fill = NULL, title = "The average technology wave in six US hubs",
		subtitle = "Average over 13 waves (see <region>.png and <region>_entrants.png); new entrants with 95% bands") +
	theme_panels + theme(legend.position = "bottom")
ggsave("hubs.png", width = 12, height = 6.5, dpi = 150)

ggplot(a[measure == "Cumulative share of the wave's companies" & years >= 0], aes(years, ratio, colour = region)) +
	geom_hline(yintercept = 1, colour = "grey55", linewidth = 0.4) +
	geom_line(linewidth = 0.9) +
	scale_colour_manual(values = c("#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300")) +
	scale_y_continuous(trans = "log2", breaks = c(2 / 3, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2), labels = function(x) paste0(signif(x, 2), "×")) +
	scale_x_continuous(breaks = 0:10, expand = expansion(mult = c(0, 0.02))) +
	labs(x = "Years since the wave took off", y = "Actual / expected companies in the region",
		colour = NULL, title = "The average technology wave in six US hubs",
		subtitle = paste0("Region's share of the wave's companies so far, relative to its share of companies with other\n",
			"product releases in the same years; average over 13 waves")) +
	theme_minimal(base_size = 11) + theme(panel.grid.minor = element_blank(), plot.title.position = "plot", legend.position = "bottom")
ggsave("hubs_cumulative.png", width = 9, height = 6, dpi = 150)
