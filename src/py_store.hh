#pragma once

#include <memory>
#include <nix/store/store-api.hh>

struct PyStore {
    std::shared_ptr<nix::Store> store;

    PyStore(std::shared_ptr<nix::Store> s) : store(std::move(s)) {}
    PyStore() = default;
};
