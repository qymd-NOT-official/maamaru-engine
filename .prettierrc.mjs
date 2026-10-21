import * as multilineArrays from "prettier-plugin-multiline-arrays";

export default {
    plugins: [
        multilineArrays,
    ],
    multilineArraysWrapThreshold: 1,
    tabWidth: 4,
    printWidth: 120,
    useTabs: false,
    bracketSameLine: true,
    bracketSpacing: false,
    endOfLine: "lf",
    overrides: [
        {
            files: [
                "**/*.yml",
                "**/*.yaml",
            ],
            options: {
                parser: "yaml",
                tabWidth: 2,
            },
        },
        {
            files: [
                "*.json",
                "*.jsonc",
            ],
            options: {
                parser: "json",
                useTabs: false,
                bracketSameLine: false,
                trailingComma: "none",
            },
        },
        {
            files: [
                "*.mts",
                "**/*.ts",
            ],
            options: {
                tabWidth: 2,
                semi: false,
                trailingComma: "all",
                bracketSpacing: true,
                singleQuote: true,
                multilineArraysWrapThreshold: -1,
            },
        },
    ],
};
