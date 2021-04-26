def serverClassFactory(serverIface):
    from .OpacityDelegate import OpacityDelegateServer
    return OpacityDelegateServer(serverIface)
