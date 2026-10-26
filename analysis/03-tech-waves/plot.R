library(data.table)
library(ggplot2)
pdf(NULL)

setwd(dirname(normalizePath(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)))))

# Pre-2011 coverage is thin; the last month is still being scraped
cv <- fread("coverage.csv")[month >= "2011-01" & month < max(month)]
labels <- c(
	mobile = "Mobile apps", cloud = "Cloud", saas = "SaaS", big_data = "Big data", iot = "Internet of things", printing_3d = "3D printing",
	wearables = "Wearables", xr = "VR / AR", metaverse = "Metaverse", crypto = "Crypto",
	genai = "Generative AI (excl. agents)", agentic = "Agentic AI", ai = "Any AI",
	cloud_broad = "Cloud", iot_broad = "Internet of things", wearables_broad = "Wearables",
	crypto_broad = "Crypto", genai_broad = "Generative AI (excl. agents)", ai_broad = "Any AI"
)
headline <- names(labels)[1:13]

# monthly.csv omits months with no matches
m <- fread("monthly.csv")
d <- m[, !c("total", "total_senders")][CJ(month = cv$month, group = c("all", "launch"), series = names(labels)), on = .(month, group, series)]
d[is.na(n), `:=`(n = 0L, senders = 0L)]
d[cv, total := fifelse(group == "all", i.kept, i.launch_kept), on = "month"]
d[unique(m[, .(month, group, total_senders)]), total_senders := i.total_senders, on = .(month, group)]
d[, date := as.IDate(paste0(month, "-01"))]
setkey(d, series, group, date)
# Centred 12-month window: ratio of sums, so thin months weigh less
d[, `:=`(rate = 1000 * n / total, smooth = 1000 * frollsum(n, 12, align = "center") / frollsum(total, 12, align = "center"),
	smooth_senders = 1000 * frollsum(senders, 12, align = "center") / frollsum(total_senders, 12, align = "center")), by = .(series, group)]
d[, label := factor(labels[series], unique(labels))]

cols <- c(all = "#2a78d6", launch = "#eb6834")
group_labels <- c(all = "All releases", launch = "Product launches")
theme_set(theme_minimal(base_size = 11) + theme(
	panel.grid.minor = element_blank(), panel.grid.major.x = element_blank(),
	legend.position = "top", legend.title = element_blank(),
	plot.background = element_rect(fill = "white", colour = NA)
))
events <- data.table(
	series = c("wearables", "xr", "xr", "metaverse", "crypto", "crypto", "crypto", "genai", "agentic", "ai", "ai"),
	date = as.IDate(c("2015-04-01", "2016-03-01", "2024-02-01", "2021-10-01", "2017-12-01", "2021-11-01", "2025-10-01", "2022-11-01", "2022-11-01", "2022-11-01", "2016-03-01"))
)
events[, label := factor(labels[series], levels(d$label))]
event_lines <- geom_vline(data = events, aes(xintercept = date), linetype = "dashed", colour = "grey60", linewidth = 0.4)
caption <- sprintf(paste(
	"PRNewswire, English releases, market-research senders excluded; header plus first 1,000 characters of the body.",
	"Launch model %s.", sep = "\n"), cv$launch_version[1])
caption_events <- paste(caption, "Dashed lines: Apple Watch release (Apr 2015); Oculus Rift (Mar 2016) and Vision Pro (Feb 2024) releases; Facebook renamed Meta (Oct 2021);",
	"bitcoin price peaks (Dec 2017, Nov 2021, Oct 2025); ChatGPT release (Nov 2022); AlphaGo vs Lee Sedol (Mar 2016, Any AI).", sep = "\n")

# shares.png: all waves; shares_core.png: mobile, crypto and AI only
for (core in c(FALSE, TRUE)) {
	keep <- if (core) c("mobile", "crypto", "genai", "agentic", "ai") else headline
	p <- ggplot(d[series %in% keep], aes(date, colour = group)) +
		geom_vline(data = events[series %in% keep], aes(xintercept = date), linetype = "dashed", colour = "grey60", linewidth = 0.4) +
		geom_line(aes(y = rate), linewidth = 0.25, alpha = 0.35) +
		geom_line(aes(y = smooth), linewidth = 0.7, na.rm = TRUE) +
		facet_wrap(~label, ncol = if (core) 1 else 2, scales = "free_y") +
		scale_colour_manual(values = cols, labels = group_labels) +
		scale_y_continuous(limits = c(0, NA)) +
		labs(x = NULL, y = "Matching releases per 1,000", caption = paste(
			if (core) paste(caption, "Dashed lines: bitcoin price peaks (Dec 2017, Nov 2021, Oct 2025); ChatGPT release (Nov 2022); AlphaGo vs Lee Sedol (Mar 2016, Any AI).", sep = "\n") else caption_events,
			"Thin: monthly. Thick: centred 12-month window.", sep = "\n"))
	ggsave(if (core) "shares_core.png" else "shares.png", p, width = if (core) 9 else 12, height = if (core) 11 else 14, dpi = 150)
}

r <- d[series %in% c("cloud", "iot", "wearables", "crypto", "genai", "ai") | grepl("_broad", series)]
r[, variant := fifelse(grepl("_broad", series), "Broad", "Strict")]
ggplot(r, aes(date, smooth, colour = group, linetype = variant)) +
	geom_vline(data = events[series %in% r$series], aes(xintercept = date), linetype = "dashed", colour = "grey60", linewidth = 0.4) +
	geom_line(linewidth = 0.6, na.rm = TRUE) +
	facet_wrap(~label, ncol = 2, scales = "free_y") +
	scale_colour_manual(values = cols, labels = group_labels) +
	scale_linetype_manual(values = c(Strict = "solid", Broad = "22")) +
	scale_y_continuous(limits = c(0, NA)) +
	labs(x = NULL, y = "Matching releases per 1,000, centred 12-month window", caption = caption_events)
ggsave("robustness.png", width = 12, height = 8, dpi = 150)

# One vote per sender and month, against one release per release: damps boilerplate from a few prolific senders
sv <- melt(d[series %in% headline], id.vars = c("series", "label", "group", "date"), measure.vars = c("smooth", "smooth_senders"), variable.name = "unit", na.rm = TRUE)
sv[, unit := factor(unit, c("smooth", "smooth_senders"), c("Releases", "Senders"))]
ggplot(sv, aes(date, value, colour = group, linetype = unit)) +
	event_lines +
	geom_line(linewidth = 0.6) +
	facet_wrap(~label, ncol = 2, scales = "free_y") +
	scale_colour_manual(values = cols, labels = group_labels) +
	scale_linetype_manual(values = c(Releases = "solid", Senders = "22")) +
	scale_y_continuous(limits = c(0, NA)) +
	labs(x = NULL, y = "Per 1,000, centred 12-month window", caption = paste(caption_events,
		"Releases: matching releases per 1,000 releases. Senders: matching sender-months per 1,000 active sender-months.", sep = "\n"))
ggsave("senders.png", width = 12, height = 14, dpi = 150)

# Lift: launch share among matches over launch share among all releases
w <- dcast(d[series %in% headline], series + label + date ~ group, value.var = c("n", "total"))
setkey(w, series, date)
w[, `:=`(lift = (frollsum(n_launch, 12, align = "center") / frollsum(n_all, 12, align = "center")) /
	(frollsum(total_launch, 12, align = "center") / frollsum(total_all, 12, align = "center")),
	matches = frollsum(n_all, 12, align = "center")), by = series]
w[matches < 50, lift := NA]
ggplot(w, aes(date, lift)) +
	event_lines +
	geom_hline(yintercept = 1, colour = "grey60", linewidth = 0.4) +
	geom_line(colour = cols[["launch"]], linewidth = 0.6, na.rm = TRUE) +
	facet_wrap(~label, ncol = 2) +
	scale_y_continuous(limits = c(0, NA)) +
	labs(x = NULL, y = "Launch share of matches / launch share of all releases",
		caption = paste(caption_events, "Centred 12-month window; windows with fewer than 50 matches left out.", sep = "\n"))
ggsave("lift.png", width = 12, height = 12, dpi = 150)

# Overview: one row per wave, ordered by the year of its all-release peak
ov <- d[series %in% headline, .(rate = 1000 * sum(n) / sum(total)), by = .(series, label, group, year = year(date))]
ov[, rel := rate / max(rate), by = .(series, group)]
peaks <- ov[group == "all", .(peak_year = year[which.max(rate)], peak = max(rate)), by = .(series, label)]
ov[peaks, row := sprintf("%s  (peak %.1f)", i.label, i.peak), on = "series"]
ov[, row := factor(row, peaks[order(peak_year, series), sprintf("%s  (peak %.1f)", label, peak)])]
ggplot(ov, aes(year, row, fill = rel)) +
	geom_tile(colour = "white", linewidth = 0.6) +
	facet_wrap(~group, labeller = as_labeller(group_labels)) +
	scale_y_discrete(limits = rev) +
	scale_fill_gradient(low = "#f0efec", high = "#2a78d6", limits = c(0, 1), name = "Rate / peak-year rate") +
	theme(panel.grid = element_blank(), legend.position = "top", legend.title = element_text()) +
	labs(x = NULL, y = NULL, caption = paste(caption, "Fill: yearly matches per 1,000 relative to the wave's peak year, within each panel. Peak: all releases, per 1,000. 2026 is partial.", sep = "\n"))
ggsave("overview.png", width = 12, height = 5.5, dpi = 150)

# Per-term timing; the Any AI panel shows only the terms not already in generative or agentic AI
tm <- fread("terms.csv")[group == "all" & series %in% headline & month %in% cv$month]
tm <- tm[!(series == "ai" & term %in% tm[series %in% c("genai", "agentic"), unique(term)])]
tm[, year := as.integer(substr(month, 1, 4))]
tm <- tm[, .(n = sum(n)), by = .(series, term, year)]
years <- cv[, unique(as.integer(substr(month, 1, 4)))]
tm <- tm[unique(tm[, .(series, term)])[, .(year = years), by = .(series, term)], on = .(series, term, year)]
tm[is.na(n), n := 0L]
tm[cv[, .(total = sum(kept)), by = .(year = as.integer(substr(month, 1, 4)))], rate := 1000 * n / i.total, on = "year"]
tm[, `:=`(rel = rate / max(rate), tot = sum(n)), by = .(series, term)]
tm[, row := sprintf("%s  (%s)", substr(term, 1, 50), format(tot, big.mark = ","))]
tm[, row := factor(row, unique(row[order(series, -tot)]))]
tm[, label := factor(labels[series], levels(d$label))]
ggplot(tm, aes(year, row, fill = rel)) +
	geom_tile(colour = "white", linewidth = 0.4) +
	facet_grid(label ~ ., scales = "free_y", space = "free_y") +
	scale_y_discrete(limits = rev) +
	scale_fill_gradient(low = "#f0efec", high = "#2a78d6", limits = c(0, 1), name = "Rate / peak-year rate") +
	theme(panel.grid = element_blank(), legend.position = "top", legend.title = element_text(), strip.text.y = element_text(angle = 0, hjust = 0)) +
	labs(x = NULL, y = NULL, caption = paste(caption, "Rows: regex term (all-release matches). Fill: yearly rate per 1,000 relative to the term's peak year.", sep = "\n"))
ggsave("terms.png", width = 11, height = 2 + 0.2 * uniqueN(tm$row), dpi = 150, limitsize = FALSE)

sink("waves.txt")
cat(sprintf("Centred 12-month rate per 1,000 releases, %s to %s. Half-peak window: first and last month at or above half the peak.\n\n", min(cv$month), max(cv$month)))
print(d[!is.na(smooth), {
	p <- which.max(smooth)
	.(peak_month = month[p], peak = round(smooth[p], 1), half_from = month[which(smooth >= smooth[p] / 2)[1]],
		half_to = month[tail(which(smooth >= smooth[p] / 2), 1)], last = round(tail(smooth, 1), 1), matches = sum(n))
}, by = .(series, group)], row.names = FALSE)
cat("\nExcluded releases by year (% of scraped, blacklisted senders already removed upstream)\n")
print(cv[, .(scraped = sum(scraped), market_research = round(100 * sum(market_research) / sum(scraped), 1),
	non_english = round(100 * sum(non_english) / sum(scraped), 1), kept = sum(kept), launch_kept = sum(launch_kept)), by = .(year = substr(month, 1, 4))], row.names = FALSE)
sink()
