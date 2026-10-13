import { describe, expect, it } from "vitest";
import { publicationBody } from "./publication";

describe("what the owner pasted as a published version", () => {
  it("reads a DOI bare, prefixed, or as a link", () => {
    expect(publicationBody("10.1145/1234567")).toEqual({ doi: "10.1145/1234567" });
    expect(publicationBody("doi:10.1145/1234567")).toEqual({ doi: "10.1145/1234567" });
    expect(publicationBody("DOI: 10.1145/1234567")).toEqual({ doi: "10.1145/1234567" });
    expect(publicationBody("https://doi.org/10.1145/1234567")).toEqual({ doi: "10.1145/1234567" });
    expect(publicationBody("http://dx.doi.org/10.1145/1234567")).toEqual({ doi: "10.1145/1234567" });
  });

  it("reads anything else as a DBLP key, a dblp.org link included", () => {
    expect(publicationBody("conf/nips/VaswaniSPUJGKP17")).toEqual({ dblp_key: "conf/nips/VaswaniSPUJGKP17" });
    expect(publicationBody("https://dblp.org/rec/conf/nips/VaswaniSPUJGKP17.html")).toEqual({ dblp_key: "conf/nips/VaswaniSPUJGKP17" });
    expect(publicationBody("  ")).toBeNull();
  });
});
