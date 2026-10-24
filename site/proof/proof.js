(function () {
  "use strict";

  const fieldNodes = Array.from(document.querySelectorAll("[data-proof-field]"));
  if (fieldNodes.length === 0) return;

  const statusNodes = document.querySelectorAll("[data-proof-status]");
  const messageNodes = document.querySelectorAll("[data-proof-message]");

  function setStatus(status, message) {
    statusNodes.forEach(function (node) {
      node.textContent = status;
    });
    messageNodes.forEach(function (node) {
      node.textContent = message;
    });
  }

  function setMissing() {
    fieldNodes.forEach(function (node) {
      node.textContent = "not published";
    });
    setStatus(
      "not published",
      "The live artifact is not published. No verification claim is being made."
    );
  }

  function displayValue(field, claims, bundle) {
    if (field === "kid") return bundle.kid;
    return claims[field];
  }

  Promise.all([
    fetch("/proof/receipt.json", { credentials: "omit" }).then(function (response) {
      if (!response.ok) throw new Error("receipt unavailable");
      return response.json();
    }),
    fetch("/proof/trust-keys.json", { credentials: "omit" }).then(function (response) {
      if (!response.ok) throw new Error("key snapshot unavailable");
      return response.json();
    }),
  ])
    .then(function (artifacts) {
      const bundle = artifacts[0];
      const keyDocument = artifacts[1];
      if (typeof bundle.signing_input !== "string") {
        throw new Error("missing signed payload");
      }
      const claims = JSON.parse(bundle.signing_input);
      const matchingKey = Array.isArray(keyDocument.keys)
        ? keyDocument.keys.some(function (key) {
            return (key.kid || key.key_id) === bundle.kid;
          })
        : false;
      if (!matchingKey) throw new Error("matching key absent");

      fieldNodes.forEach(function (node) {
        const value = displayValue(node.dataset.proofField, claims, bundle);
        node.textContent = value === undefined || value === null ? "not signed" : String(value);
      });
      setStatus(
        "artifacts loaded",
        "The receipt and matching key snapshot loaded. Run the offline verifier for a cryptographic verdict."
      );
    })
    .catch(setMissing);
})();
