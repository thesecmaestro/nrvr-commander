#!/usr/bin/python


from collections import namedtuple
import ntpath
import os.path
import posixpath
import random
import re
import shutil
import string
import sys
import tempfile
import time
import wingdbstub

from nrvr.diskimage.isoimage import IsoImage, IsoImageModificationFromString, IsoImageModificationFromPath
from nrvr.distros.common.ssh import LinuxSshCommand
from nrvr.distros.common.util import LinuxUtil
from nrvr.distros.el.gnome import ElGnome
from nrvr.distros.el.kickstart import ElIsoImage, ElKickstartFileContent
from nrvr.distros.el.kickstarttemplates import ElKickstartTemplates
from nrvr.distros.el.util import ElUtil
from nrvr.distros.ub.util import UbUtil
from nrvr.distros.ub.rel1204.gnome import Ub1204Gnome
from nrvr.distros.ub.rel1204.kickstart import Ub1204IsoImage, UbKickstartFileContent
from nrvr.distros.ub.rel1204.kickstarttemplates import UbKickstartTemplates
from nrvr.distros.ub.rel1404.gnome import Ub1404Gnome
from nrvr.distros.ub.rel1404.preseed import Ub1404IsoImage, UbPreseedFileContent
from nrvr.distros.ub.rel1404.preseedtemplates import UbPreseedTemplates
from nrvr.machine.ports import PortsFile
from nrvr.process.commandcapture import CommandCapture
from nrvr.remote.ssh import SshCommand, ScpCommand
from nrvr.util.download import Download
from nrvr.util.ipaddress import IPAddress
from nrvr.util.nameserver import Nameserver
from nrvr.util.registering import RegisteringUser
from nrvr.util.requirements import SystemRequirements
from nrvr.util.times import Timestamp
from nrvr.util.user import ScriptUser
from nrvr.vm.vmware import VmdkFile, VmxFile, VMwareHypervisor, VMwareMachine
from nrvr.vm.vmwaretemplates import VMwareTemplates
from nrvr.wins.common.autounattend import WinUdfImage
from nrvr.wins.common.cygwin import CygwinDownload
from nrvr.wins.common.javaw import JavawDownload
from nrvr.wins.common.ssh import CygwinSshCommand
from nrvr.wins.win7.autounattend import Win7UdfImage, Win7AutounattendFileContent
from nrvr.wins.win7.autounattendtemplates import Win7AutounattendTemplates

# this is a good way to preflight check
SystemRequirements.commandsRequiredByImplementations([IsoImage, WinUdfImage,
                                                      VmdkFile, VMwareHypervisor,
                                                      SshCommand, ScpCommand,
                                                      CygwinDownload, JavawDownload],
                                                     verbose=True)
# this is a good way to preflight check
VMwareHypervisor.localRequired()
VMwareHypervisor.snapshotsRequired()

# will modulo over machinesPattern,
# customize as needed
testVmsRange = range(181, 182) 

# customize as needed
# normally at least one
testUsers = [RegisteringUser(username="tester", pwd="testing"),
             RegisteringUser(username="tester2", pwd="testing")
             ]

MachineParameters = namedtuple("MachineParameters", ["distro", "arch", "browser", "lang", "memsize", "cores"])
class Arch(str): pass # make sure it is a string to avoid string-number unequality

machinesPattern = [
                   MachineParameters(distro="win", arch=Arch(32), browser="iexplorer", lang="en-US", memsize=1020, cores=1)
                   ]

# trying to approximate the order in which identifiers are used from this tuple
VmIdentifiers = namedtuple("VmIdentifiers", ["vmxFilePath", "name", "number", "ipaddress", "mapas"])

# customize as needed
def vmIdentifiersForNumber(number, index):
    """Make various identifiers for a virtual machine.
    
    number
        an int probably best between 2 and 254.
    
    Return a VmIdentifiers instance."""
    # this is the order in which identifiers are derived
    #
    # will use hostonly on eth1
    ipaddress = IPAddress.numberWithinSubnet(VMwareHypervisor.localHostOnlyIPAddress, number)
    name = IPAddress.nameWithNumber("testvm", ipaddress, separator=None)
    vmxFilePath = ScriptUser.loggedIn.userHomeRelative("vmware/testvms/%s/%s.vmx" % (name, name))
    indexModulo = index % len(machinesPattern)
    mapas = machinesPattern[indexModulo]
    return VmIdentifiers(vmxFilePath=vmxFilePath,
                         name=name,
                         number=number,
                         ipaddress=ipaddress,
                         mapas=mapas)

#testVmsIdentifiers = map(lambda number: vmIdentifiersForNumber(number), testVmsRange)
testVmsIdentifiers = []
for index, number in enumerate(testVmsRange):
    testVmsIdentifiers.append(vmIdentifiersForNumber(number, index))

def installToolsIntoTestVm(vmIdentifiers, forceThisStep=False):
    testVm = VMwareMachine(vmIdentifiers.vmxFilePath)
    distro = vmIdentifiers.mapas.distro
    arch = vmIdentifiers.mapas.arch
    browser = vmIdentifiers.mapas.browser
    #
 
    rootOrAnAdministrator = testVm.regularUser
    #
    snapshots = VMwareHypervisor.local.listSnapshots(vmIdentifiers.vmxFilePath)
    snapshotExists = "tools installed" in snapshots
    if not snapshotExists or forceThisStep:
        if VMwareHypervisor.local.isRunning(testVm.vmxFilePath):
            testVm.shutdownCommand(ignoreException=True)
            VMwareHypervisor.local.sleepUntilNotRunning(testVm.vmxFilePath, ticker=True)
        VMwareHypervisor.local.revertToSnapshotAndDeleteDescendants(vmIdentifiers.vmxFilePath, "OS installed")
        #
        # start up until successful login into GUI
        VMwareHypervisor.local.start(testVm.vmxFilePath, gui=True, extraSleepSeconds=0)
        userSshParameters = testVm.sshParameters(user=testVm.regularUser)
        if distro in ["sl", "cent", "ub1204", "ub1404"]:
            LinuxSshCommand.sleepUntilIsGuiAvailable(userSshParameters, ticker=True)
        elif distro == "win":
            CygwinSshCommand.sleepUntilIsGuiAvailable(userSshParameters, ticker=True)
        #
        # a necessity on some
        # international version OS
        testVm.sshCommand(["mkdir -p ~/Downloads"], user=testVm.regularUser)
        if distro == "win":
            testVm.sshCommand(['mkdir -p "$( cygpath -u "$USERPROFILE/Downloads" )"'], user=testVm.regularUser)
            echo = testVm.sshCommand(['echo "$( cygpath -u "$USERPROFILE/Downloads" )"'], user=testVm.regularUser)
                        
        # install Java
        if distro == "win":
            waitingForJavawInstallerSuccess = True
            # Need to reboot before path takes effect SW
            # must restart for change of PATH to be effective
            # shut down
            testVm.shutdownCommand()
            if VMwareHypervisor.isRunning(VMwareHypervisor.local, testVm.vmxFilePath):
                print "Expect Is Running " + testVm.vmxFilePath
            else:   
                print "Don't expect this..we just shutting it down..Is NOT Running " + testVm.vmxFilePath            
            VMwareHypervisor.local.sleepUntilNotRunning(testVm.vmxFilePath, ticker=True)
            # start up until successful login into GUI
            VMwareHypervisor.local.start(testVm.vmxFilePath, gui=True, extraSleepSeconds=0)
            
      

# make sure virtual machines are no longer running from previous activities if any
for vmIdentifiers in testVmsIdentifiers:
    VMwareHypervisor.local.notRunningRequired(vmIdentifiers.vmxFilePath)

testVms = []

#for vmIdentifiers in testVmsIdentifiers:
#    testVm = makeTestVmWithGui(vmIdentifiers)
#    testVms.append(testVm)

for vmIdentifiers in testVmsIdentifiers:
    testVm = installToolsIntoTestVm(vmIdentifiers) #, forceThisStep=True)

print "DONE for now, processed %s" % (", ".join(map(lambda vmdentifier: vmdentifier.name, testVmsIdentifiers)))
