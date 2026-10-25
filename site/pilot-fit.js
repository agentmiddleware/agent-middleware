(function () {
  "use strict";

  const calculator = document.querySelector(".pilot-fit");
  if (!calculator) return;

  const fields = Array.from(calculator.querySelectorAll("input"));
  const summary = calculator.querySelector("[data-fit-summary]");
  const guidance = calculator.querySelector("[data-fit-guidance]");
  const next = calculator.querySelector("[data-fit-next]");
  const alternative = calculator.querySelector("[data-fit-alternative]");
  const money = new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  });
  const roundedAmount = new Intl.NumberFormat("en-US", {
    useGrouping: false,
    maximumFractionDigits: 2,
  });

  function update() {
    next.hidden = true;
    alternative.hidden = true;
    guidance.textContent = "";
    for (const field of fields) {
      const invalid = field.validity.badInput ||
        (field.value !== "" && (!field.checkValidity() ||
          !Number.isFinite(field.valueAsNumber)));
      field.setAttribute("aria-invalid", String(invalid));
    }
    const invalid = fields.find((field) => field.getAttribute("aria-invalid") === "true");
    if (invalid) {
      summary.textContent = "Check your inputs: use zero or positive numbers, whole actions, and percentages from 0 to 100.";
      return;
    }
    if (fields.some((field) => field.required && field.value === "")) {
      summary.textContent = "Enter the four workflow assumptions to estimate avoided loss.";
      return;
    }

    const [actions, rate, loss, reduction, cost] = fields.map((field) => field.valueAsNumber);
    const avoidedDuplicates = actions * (rate / 100) * (reduction / 100);
    const avoidedLoss = avoidedDuplicates * loss;
    const hasCost = fields[4].value !== "";
    const threshold = hasCost && avoidedDuplicates > 0 ? cost / avoidedDuplicates : 0;
    if (![avoidedDuplicates, avoidedLoss, threshold].every(Number.isFinite)) {
      summary.textContent = "These values are too large to calculate. Use smaller estimates.";
      return;
    }

    summary.textContent = "Estimated avoided duplicate loss: " + money.format(avoidedLoss) + " / month.";
    if (avoidedDuplicates === 0) {
      guidance.textContent = "These assumptions imply no avoided duplicates, so duplicate prevention alone cannot justify an added cost.";
      alternative.hidden = false;
    } else if (!hasCost) {
      guidance.textContent = "Economic fit is still unknown. Add your total monthly cost to compare; a service quote and operating scope are needed before deciding.";
    } else {
      const comparison = " Break even loss per duplicate: " + money.format(threshold) + ".";
      // Use the same decimal rounding as the displayed currency amounts.
      const displayedLoss = Number(roundedAmount.format(avoidedLoss));
      const displayedCost = Number(roundedAmount.format(cost));
      if (displayedLoss > displayedCost) {
        guidance.textContent = "Estimated avoided loss exceeds total monthly cost by " + money.format(displayedLoss - displayedCost) + "." + comparison + " This is a reason to validate the assumptions in a paid pilot, not evidence of savings.";
        next.hidden = false;
      } else {
        guidance.textContent = "Estimated avoided loss does not exceed total monthly cost." + comparison + " Start with simpler reliability or upstream idempotency controls. Any separate operational benefit needs its own evidence.";
        alternative.hidden = false;
      }
    }
  }

  calculator.addEventListener("input", update);
  calculator.querySelector("[data-fit-inputs]").hidden = false;
  calculator.querySelector("[data-fit-result]").hidden = false;
  update();
})();
