library(data.table)
library(ggplot2)
library(sandwich)
library(lmtest)
pdf(NULL)

setwd(dirname(normalizePath(sub("--file=", "", grep("--file=", commandArgs(), value = TRUE)))))

# FRED series; "growth" = YoY log change, "change" = YoY difference (rates, indexes centred on 0)
ind <- fread(text = "
id,label,transform
GDPC1,Real GDP,growth
INDPRO,Industrial production,growth
PAYEMS,Nonfarm payrolls,growth
UNRATE,Unemployment rate,change
RRSFS,Real retail sales,growth
NEWORDER,Core capital goods orders,growth
Y001RC1Q027SBEA,Investment in IP products,growth
BABATOTALSAUS,Business applications,growth
CPIAUCSL,CPI,growth
FEDFUNDS,Fed funds rate,change
GS10,10-year Treasury yield,change
NASDAQCOM,Nasdaq Composite,growth
VIXCLS,VIX,change
NFCI,Financial conditions (NFCI),change
UMCSENT,Consumer sentiment,change
USEPUINDXM,Policy uncertainty (EPU),growth
")

if (!file.exists("fred.csv")) {
	fred <- rbindlist(lapply(ind$id, function(id) {
		x <- fread(sprintf("https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s&cosd=2009-01-01", id), na.strings = ".")
		data.table(id = id, date = as.IDate(x[[1]]), value = as.numeric(x[[2]]))
	}))
	fwrite(fred, "fred.csv")
}
fred <- fread("fred.csv")[!is.na(value)]
fred[, q := as.IDate(sprintf("%d-%02d-01", year(date), 3 * quarter(date) - 2))]

# Macro: quarterly mean, then YoY transform
macro <- fred[, .(value = mean(value)), by = .(id, q)][ind, on = "id"]
setkey(macro, id, q)
stopifnot(macro[, all(diff(q) %between% c(89, 92)), by = id]$V1)
macro[, x := if (transform[1] == "growth") log(value) - shift(log(value), 4) else value - shift(value, 4), by = id]

# Releases: complete quarters from 01-release-counts; share YoY = launch YoY - total YoY
m <- fread("../01-release-counts/monthly.csv")[month >= "2011-01" & month < max(month)]
m[, date := as.IDate(paste0(month, "-01"))]
rel <- m[, .(total = as.numeric(sum(total)), launch = as.numeric(sum(launch)), months = .N), by = .(q = as.IDate(sprintf("%d-%02d-01", year(date), 3 * quarter(date) - 2)))][months == 3]
rel[, share := launch / total]
rel <- melt(rel, id.vars = "q", measure.vars = c("total", "launch", "share"), variable.name = "series", value.name = "level")
setkey(rel, series, q)
rel[, y := log(level) - shift(log(level), 4), by = series]
rel[, series := factor(series, c("total", "launch", "share"), c("All releases", "Product launches", "Launch share"))]

# Lag k > 0: macro leads releases by k quarters
lags <- -4:4
macro_lagged <- macro[, c(.(q = q), setNames(shift(x, lags), lags)), by = .(id, label)]
macro_lagged <- melt(macro_lagged, id.vars = c("id", "label", "q"), variable.name = "lag", value.name = "x")
macro_lagged[, lag := as.integer(as.character(lag))]

d <- rel[macro_lagged, on = "q", allow.cartesian = TRUE, nomatch = NULL][!is.na(y) & !is.na(x)]
d[, covid := q >= as.IDate("2020-01-01") & q <= as.IDate("2021-12-01")]
fwrite(d[lag == 0, .(q, series, y, id, label, x)], "quarterly.csv")

# Pearson r with Newey-West p-value (overlapping YoY changes are autocorrelated)
est <- rbindlist(lapply(c(FALSE, TRUE), function(drop_covid) {
	d[!(drop_covid & covid), {
		fit <- lm(scale(y) ~ scale(x))
		.(r = cor(y, x), p = coeftest(fit, vcov. = NeweyWest(fit, lag = 4, prewhite = FALSE))[2, 4], n = .N)
	}, by = .(series, id, label, lag)][, sample := if (drop_covid) "Excl. 2020-21" else "Full sample"]
}))
est[lag == 0, q_bh := p.adjust(p, "BH"), by = .(sample, series)]
est[, label := factor(label, rev(ind$label))]
est[, sample := factor(sample, c("Full sample", "Excl. 2020-21"))]

sink("correlations.txt")
cat(sprintf("Quarterly YoY changes, %s to %s. r = Pearson; p = Newey-West (4 lags); q = BH within sample x series.\n",
	min(d$q), max(d$q)))
cat("Lag k > 0: macro leads releases by k quarters.\n\n")
for (s in levels(est$sample)) {
	cat("==", s, "== contemporaneous\n")
	print(dcast(est[sample == s & lag == 0], label ~ series, value.var = c("r", "q_bh"), fun.aggregate = function(v) round(v, 2)), row.names = FALSE)
	cat("\nLag with largest |r| (n at lag 0:", est[sample == s & lag == 0, max(n)], ")\n")
	print(est[sample == s, .SD[which.max(abs(r))], by = .(series, label)][order(series, -abs(r)), .(series, label, lag, r = round(r, 2), p = round(p, 3))], row.names = FALSE)
	cat("\n")
}
sink()

cols <- c("All releases" = "#2a78d6", "Product launches" = "#eb6834", "Launch share" = "#1baf7a")
theme_set(theme_minimal(base_size = 11) + theme(
	panel.grid.minor = element_blank(),
	legend.position = "top", legend.title = element_blank(),
	plot.background = element_rect(fill = "white", colour = NA)
))
caption <- "Quarterly YoY changes of PRNewswire release counts and FRED indicators."

ggplot(est[lag == 0], aes(series, label, fill = r)) +
	geom_tile(colour = "white", linewidth = 1) +
	geom_text(aes(label = sprintf("%.2f%s", r, ifelse(q_bh < 0.05, "**", ifelse(q_bh < 0.1, "*", "")))), size = 3.2) +
	facet_wrap(~sample) +
	scale_fill_gradient2(low = "#d03b3b", mid = "#f0efec", high = "#2a78d6", limits = c(-1, 1)) +
	theme(panel.grid = element_blank(), legend.position = "right", legend.title = element_text()) +
	labs(x = NULL, y = NULL, caption = paste(caption, "Same quarter. * BH q < 0.1, ** q < 0.05 (Newey-West p-values)."))
ggsave("heatmap.png", width = 9, height = 6.5, dpi = 150)

ggplot(est, aes(lag, r, colour = series, linetype = sample)) +
	geom_hline(yintercept = 0, colour = "grey60", linewidth = 0.4) +
	geom_vline(xintercept = 0, colour = "grey85", linewidth = 0.4) +
	geom_line(linewidth = 0.5) +
	geom_point(aes(shape = p < 0.05), size = 1.6) +
	facet_wrap(~factor(label, ind$label), ncol = 4) +
	scale_colour_manual(values = cols) +
	scale_shape_manual(values = c(`FALSE` = 1, `TRUE` = 16), labels = c("p >= 0.05", "p < 0.05")) +
	scale_x_continuous(breaks = c(-4, 0, 4)) +
	labs(x = "Lag (quarters; > 0 = indicator leads)", y = "Correlation", caption = paste(caption, "Unadjusted Newey-West p-values."))
ggsave("ccf.png", width = 11, height = 10, dpi = 150)

z <- d[lag == 0 & series == "Launch share"][, `:=`(y = (y - mean(y)) / sd(y), x = (x - mean(x)) / sd(x)), by = id]
ggplot(z, aes(q)) +
	geom_hline(yintercept = 0, colour = "grey60", linewidth = 0.4) +
	geom_line(aes(y = x, colour = "Indicator"), linewidth = 0.5) +
	geom_line(aes(y = y, colour = "Launch share"), linewidth = 0.5) +
	facet_wrap(~factor(label, ind$label), ncol = 4) +
	scale_colour_manual(values = c("Indicator" = "grey45", "Launch share" = "#1baf7a")) +
	coord_cartesian(ylim = c(-4, 4)) +
	labs(x = NULL, y = "z-score of YoY change", caption = paste(caption, "Clipped at +/- 4 sd."))
ggsave("series.png", width = 11, height = 9, dpi = 150)
