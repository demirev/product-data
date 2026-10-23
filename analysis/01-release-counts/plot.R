library(data.table)
library(ggplot2)
pdf(NULL)

setwd(dirname(normalizePath(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)))))

d <- fread("monthly.csv")
# Pre-2011 coverage is thin; the last month is still being scraped
d <- d[month >= "2011-01" & month < max(month)]
d[, date := as.IDate(paste0(month, "-01"))]

long <- melt(d, id.vars = "date", measure.vars = c("total", "launch"), variable.name = "series", value.name = "n")
long[, series := factor(series, c("total", "launch"), c("All releases", "Product launches"))]
stopifnot(uniqueN(long$date) == length(seq(min(long$date), max(long$date), by = "month")))

# Index base: mean of the 12 months before Nov 2022
base_from <- as.IDate("2021-11-01")
base_to <- as.IDate("2022-10-01")
long[, base := mean(n[date >= base_from & date <= base_to]), by = series]
long[, index := 100 * n / base]

# Smoothers: centred 3-month and trailing 12-month means, and the trend of a robust STL on log counts
# (robust weights discount months with scraping gaps)
smooth <- long[, .(
	date,
	`3-month centred mean` = frollmean(n, 3, align = "center"),
	`12-month trailing mean` = frollmean(n, 12),
	`STL trend (robust, log)` = exp(stl(ts(log(n), frequency = 12), s.window = 13, robust = TRUE)$time.series[, "trend"]),
	base
), by = series]
smooth <- melt(smooth, id.vars = c("series", "date", "base"), variable.name = "method", value.name = "n", na.rm = TRUE)
smooth[, index := 100 * n / base]

cols <- c("All releases" = "#2a78d6", "Product launches" = "#eb6834")
chatgpt <- geom_vline(xintercept = as.IDate("2022-11-01"), linetype = "dashed", colour = "grey60", linewidth = 0.4)
theme_set(theme_minimal(base_size = 11) + theme(
	panel.grid.minor = element_blank(), panel.grid.major.x = element_blank(),
	legend.position = "top", legend.title = element_blank(),
	plot.background = element_rect(fill = "white", colour = NA)
))
caption <- sprintf("PRNewswire, monthly by publication date. Launch model %s. Dashed line: Nov 2022.", d$launch_version[1])
base_band <- annotate("rect", xmin = base_from, xmax = base_to, ymin = -Inf, ymax = Inf, fill = "grey90")
index_caption <- paste(caption, "Shaded: index base, Nov 2021 to Oct 2022 = 100.", sep = "\n")

ggplot(long, aes(date, n, colour = series)) +
	chatgpt +
	geom_line(linewidth = 0.5) +
	facet_wrap(~series, ncol = 1, scales = "free_y") +
	scale_colour_manual(values = cols, guide = "none") +
	scale_y_continuous(labels = scales::comma, limits = c(0, NA)) +
	labs(x = NULL, y = "Releases per month", caption = caption)
ggsave("counts.png", width = 9, height = 6, dpi = 150)

ggplot(long, aes(date, index, colour = series)) +
	base_band +
	geom_hline(yintercept = 100, colour = "grey60", linewidth = 0.4) +
	chatgpt +
	geom_line(linewidth = 0.5) +
	scale_colour_manual(values = cols) +
	labs(x = NULL, y = "Index (base = 100)", caption = index_caption)
ggsave("index.png", width = 9, height = 5, dpi = 150)

ggplot(smooth, aes(date, index, colour = series)) +
	base_band +
	geom_hline(yintercept = 100, colour = "grey60", linewidth = 0.4) +
	chatgpt +
	geom_line(linewidth = 0.6) +
	facet_wrap(~method, ncol = 1) +
	scale_colour_manual(values = cols) +
	labs(x = NULL, y = "Index (base = 100)", caption = index_caption)
ggsave("smooth.png", width = 9, height = 9, dpi = 150)

ggplot(smooth[method == "12-month trailing mean"], aes(date, index, colour = series)) +
	base_band +
	geom_hline(yintercept = 100, colour = "grey60", linewidth = 0.4) +
	chatgpt +
	geom_line(linewidth = 0.6) +
	scale_colour_manual(values = cols) +
	labs(x = NULL, y = "Index (base = 100), 12-month trailing mean", caption = index_caption)
ggsave("smooth_12m.png", width = 9, height = 5, dpi = 150)
