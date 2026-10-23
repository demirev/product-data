library(data.table)
library(ggplot2)
pdf(NULL)

setwd(dirname(normalizePath(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)))))

# Pre-2011 coverage is thin; the last month is still being scraped
cv <- fread("coverage.csv")[month >= "2011-01" & month < max(month)]
labels <- c(
	mobile = "Mobile apps", crypto = "Crypto", genai = "Generative AI (excl. agents)", agentic = "Agentic AI", ai = "Any AI",
	crypto_broad = "Crypto", genai_broad = "Generative AI (excl. agents)", ai_broad = "Any AI"
)
headline <- c("mobile", "crypto", "genai", "agentic", "ai")

# monthly.csv omits months with no matches
d <- fread("monthly.csv")[, !"total"][CJ(month = cv$month, group = c("all", "launch"), series = names(labels)), on = .(month, group, series)]
d[is.na(n), `:=`(n = 0L, senders = 0L)]
d[cv, total := fifelse(group == "all", i.kept, i.launch_kept), on = "month"]
d[, date := as.IDate(paste0(month, "-01"))]
setkey(d, series, group, date)
# Centred 12-month window: ratio of sums, so thin months weigh less
d[, `:=`(rate = 1000 * n / total, smooth = 1000 * frollsum(n, 12, align = "center") / frollsum(total, 12, align = "center")), by = .(series, group)]
d[, label := factor(labels[series], unique(labels))]

cols <- c(all = "#2a78d6", launch = "#eb6834")
group_labels <- c(all = "All releases", launch = "Product launches")
theme_set(theme_minimal(base_size = 11) + theme(
	panel.grid.minor = element_blank(), panel.grid.major.x = element_blank(),
	legend.position = "top", legend.title = element_blank(),
	plot.background = element_rect(fill = "white", colour = NA)
))
events <- data.table(series = c("crypto", "crypto", "crypto", "genai", "agentic", "ai"), date = as.IDate(c("2017-12-01", "2021-11-01", "2025-10-01", "2022-11-01", "2022-11-01", "2022-11-01")))
events[, label := factor(labels[series], levels(d$label))]
event_lines <- geom_vline(data = events, aes(xintercept = date), linetype = "dashed", colour = "grey60", linewidth = 0.4)
caption <- sprintf(paste(
	"PRNewswire, English releases, market-research senders excluded; header plus first 1,000 characters of the body.",
	"Launch model %s.", sep = "\n"), cv$launch_version[1])
caption_events <- paste(caption, "Dashed lines: bitcoin price peaks (Dec 2017, Nov 2021, Oct 2025), ChatGPT release (Nov 2022).")

ggplot(d[series %in% headline], aes(date, colour = group)) +
	event_lines +
	geom_line(aes(y = rate), linewidth = 0.25, alpha = 0.35) +
	geom_line(aes(y = smooth), linewidth = 0.7, na.rm = TRUE) +
	facet_wrap(~label, ncol = 1, scales = "free_y") +
	scale_colour_manual(values = cols, labels = group_labels) +
	scale_y_continuous(limits = c(0, NA)) +
	labs(x = NULL, y = "Matching releases per 1,000", caption = paste(caption_events, "Thin: monthly. Thick: centred 12-month window.", sep = "\n"))
ggsave("shares.png", width = 9, height = 11, dpi = 150)

r <- d[series %in% c("crypto", "crypto_broad", "genai", "genai_broad", "ai", "ai_broad")]
r[, variant := fifelse(grepl("_broad", series), "Broad", "Strict")]
ggplot(r, aes(date, smooth, colour = group, linetype = variant)) +
	geom_vline(data = events[series %in% r$series], aes(xintercept = date), linetype = "dashed", colour = "grey60", linewidth = 0.4) +
	geom_line(linewidth = 0.6, na.rm = TRUE) +
	facet_wrap(~label, ncol = 1, scales = "free_y") +
	scale_colour_manual(values = cols, labels = group_labels) +
	scale_linetype_manual(values = c(Strict = "solid", Broad = "22")) +
	scale_y_continuous(limits = c(0, NA)) +
	labs(x = NULL, y = "Matching releases per 1,000, centred 12-month window", caption = caption_events)
ggsave("robustness.png", width = 9, height = 8, dpi = 150)

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
	facet_wrap(~label, ncol = 1) +
	scale_y_continuous(limits = c(0, NA)) +
	labs(x = NULL, y = "Launch share of matches / launch share of all releases",
		caption = paste(caption_events, "Centred 12-month window; windows with fewer than 50 matches left out.", sep = "\n"))
ggsave("lift.png", width = 9, height = 9, dpi = 150)

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
ggsave("terms.png", width = 11, height = 16, dpi = 150)

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
